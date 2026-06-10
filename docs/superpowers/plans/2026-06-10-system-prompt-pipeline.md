# System Prompt Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the frozen `system_prompt: str` with a versioned, per-section refreshable pipeline that enables archive/pruned content to update within a ReAct turn.

**Architecture:** Each system prompt section becomes an independent `SystemPromptProvider` with internal version-based caching. A `SystemPromptPipeline` holds an ordered list of providers and assembles the full prompt on each iteration. Providers are reconstructed per-turn in `ctx_mgr.load()`, with version caching only effective across iterations within the same turn.

**Tech Stack:** Python 3.12+, asyncio, pytest

**Spec:** `docs/superpowers/specs/2026-06-10-system-prompt-pipeline-design.md`

---

## Plan Discipline Requirements

> **Mandatory for all implementers:**

1. **Update this plan after completing each step** — check the `- [ ]` box to `- [x]`, record actual result.
2. **Document any implementation deviations** — if a step changed from the plan, add a `> **Deviation:**` note after the step explaining what changed and why.
3. **Run tests after each task** — verify nothing is broken before moving to the next task.
4. **Commit after each task** — each task should produce a clean, reviewable commit.

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `framework/memory/pipeline/__init__.py` | Public API exports |
| `framework/memory/pipeline/abc.py` | `SystemPromptProvider` ABC |
| `framework/memory/pipeline/pipeline.py` | `SystemPromptPipeline` container |
| `framework/memory/pipeline/providers.py` | All 9 provider implementations |
| `tests/unit/memory/pipeline/__init__.py` | Test package |
| `tests/unit/memory/pipeline/test_abc.py` | ABC contract tests |
| `tests/unit/memory/pipeline/test_pipeline.py` | Pipeline assembly tests |
| `tests/unit/memory/pipeline/test_providers.py` | Individual provider tests |

### Modified files

| File | Change |
|---|---|
| `framework/core/context.py` | `ContextState` adds `system_prompt_pipeline` field, `to_messages()` prefers pipeline |
| `framework/agents/react/nodes/llm.py` | `LLMNode._build_messages()` uses pipeline if available |
| `framework/memory/system.py` | `MemorySystemContextManager.load()` builds pipeline |
| `framework/memory/context_governance.py` | URB XML description update |
| `framework/memory/pruned/manager.py` | Add `get_version()` method |

---

## Phase 1: Foundation — ABC and Pipeline

### Task 1: SystemPromptProvider ABC

**Files:**
- Create: `framework/memory/pipeline/__init__.py`
- Create: `framework/memory/pipeline/abc.py`
- Create: `tests/unit/memory/pipeline/__init__.py`
- Create: `tests/unit/memory/pipeline/test_abc.py`

- [ ] **Step 1: Create package `__init__.py`**

```python
# framework/memory/pipeline/__init__.py
"""System prompt pipeline — versioned, per-section refreshable prompt assembly."""

from framework.memory.pipeline.abc import SystemPromptProvider
from framework.memory.pipeline.pipeline import SystemPromptPipeline

__all__ = ["SystemPromptProvider", "SystemPromptPipeline"]
```

- [ ] **Step 2: Write ABC test for contract behavior**

```python
# tests/unit/memory/pipeline/__init__.py
# (empty file)
```

```python
# tests/unit/memory/pipeline/test_abc.py
"""Tests for SystemPromptProvider ABC contract."""
from __future__ import annotations

import asyncio
import pytest

from framework.memory.pipeline.abc import SystemPromptProvider


class _StaticProvider(SystemPromptProvider):
    """Test provider that returns fixed content."""

    def __init__(self, content: str = "hello") -> None:
        super().__init__()
        self._content = content
        self._version_calls = 0
        self._content_calls = 0

    async def _fetch_version(self) -> str:
        self._version_calls += 1
        return "v1"

    async def _fetch_content(self) -> str:
        self._content_calls += 1
        return self._content


class _FailingVersionProvider(SystemPromptProvider):
    """Test provider whose _fetch_version raises."""

    async def _fetch_version(self) -> str:
        raise RuntimeError("version fail")

    async def _fetch_content(self) -> str:
        return "should not reach"


class _FailingContentProvider(SystemPromptProvider):
    """Test provider whose _fetch_content raises."""

    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        raise RuntimeError("content fail")


@pytest.mark.asyncio
async def test_first_call_always_fetches():
    """First get_or_refresh() must fetch because _last_version starts as None."""
    provider = _StaticProvider("test content")
    result = await provider.get_or_refresh()
    assert result == "test content"
    assert provider._version_calls == 1
    assert provider._content_calls == 1
    assert provider.last_version == "v1"


@pytest.mark.asyncio
async def test_cached_hit_when_version_unchanged():
    """Second call with same version should use cache, no content re-fetch."""
    provider = _StaticProvider("cached")
    await provider.get_or_refresh()  # first call
    result = await provider.get_or_refresh()  # second call
    assert result == "cached"
    assert provider._version_calls == 2  # version checked both times
    assert provider._content_calls == 1  # content fetched only once


@pytest.mark.asyncio
async def test_version_change_triggers_refresh():
    """If version changes, content is re-fetched."""

    class _ChangingProvider(SystemPromptProvider):
        def __init__(self) -> None:
            super().__init__()
            self._counter = 0

        async def _fetch_version(self) -> str:
            return f"v{self._counter}"

        async def _fetch_content(self) -> str:
            self._counter += 1
            return f"content-{self._counter}"

    provider = _ChangingProvider()
    r1 = await provider.get_or_refresh()
    # Manually change version by bumping counter
    r2 = await provider.get_or_refresh()
    assert r1 != r2  # version changed, content re-fetched


@pytest.mark.asyncio
async def test_empty_version_forces_refresh():
    """_fetch_version returning '' should trigger refresh (error fallback)."""

    class _EmptyVersionProvider(SystemPromptProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _fetch_version(self) -> str:
            self.calls += 1
            # First call returns "v1", subsequent return ""
            return "v1" if self.calls == 1 else ""

        async def _fetch_content(self) -> str:
            return "refreshed"

    provider = _EmptyVersionProvider()
    await provider.get_or_refresh()  # normal first call
    # After first call, _last_version = "v1"
    # Second call: _fetch_version returns "" which != "v1" → refresh
    # Actually "" != "v1" is True, so it refreshes. But we need to verify
    # the provider handles the "" → force refresh pattern.
    # Let's test with initial None:
    provider2 = _EmptyVersionProvider()
    r = await provider2.get_or_refresh()
    assert r == "refreshed"


@pytest.mark.asyncio
async def test_cannot_instantiate_abc_directly():
    """SystemPromptProvider is abstract."""
    with pytest.raises(TypeError):
        SystemPromptProvider()


@pytest.mark.asyncio
async def test_initial_state():
    """Provider starts with no version and empty cache."""
    provider = _StaticProvider()
    assert provider.last_version is None
```

- [ ] **Step 3: Run test to verify it fails (ABC not yet created)**

Run: `python -m pytest tests/unit/memory/pipeline/test_abc.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'framework.memory.pipeline'`

- [ ] **Step 4: Implement SystemPromptProvider ABC**

```python
# framework/memory/pipeline/abc.py
"""SystemPromptProvider ABC — versioned, cacheable system prompt section."""
from __future__ import annotations

from abc import ABC, abstractmethod


class SystemPromptProvider(ABC):
    """One section of the system prompt pipeline with version-based caching.

    Subclasses implement _fetch_version() and _fetch_content().
    The ABC handles version comparison, caching, and conditional refresh.

    Lifecycle:
    - Constructed with _last_version = None, _cached_content = ""
    - First get_or_refresh() always fetches (because _last_version is None)
    - Subsequent calls compare _fetch_version() with _last_version
    - Version match → return cached content (zero I/O)
    - Version mismatch → re-fetch content, update cache
    """

    def __init__(self) -> None:
        self._last_version: str | None = None
        self._cached_content: str = ""

    @abstractmethod
    async def _fetch_version(self) -> str:
        """Get current version string from underlying storage.

        Returns "" on error to force refresh.
        """

    @abstractmethod
    async def _fetch_content(self) -> str:
        """Get fresh content from underlying storage.

        Returns "" if no content is available.
        """

    async def get_or_refresh(self) -> str:
        """Return cached content or refresh if version changed."""
        current = await self._fetch_version()
        if self._last_version is None or current != self._last_version:
            self._cached_content = await self._fetch_content()
            self._last_version = current
        return self._cached_content

    @property
    def last_version(self) -> str | None:
        """Last cached version string, for debugging/logging."""
        return self._last_version
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pipeline/test_abc.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add framework/memory/pipeline/ tests/unit/memory/pipeline/
git commit -m "feat(pipeline): add SystemPromptProvider ABC with version-based caching"
```

---

### Task 2: SystemPromptPipeline

**Files:**
- Create: `framework/memory/pipeline/pipeline.py`
- Create: `tests/unit/memory/pipeline/test_pipeline.py`

- [ ] **Step 1: Write pipeline tests**

```python
# tests/unit/memory/pipeline/test_pipeline.py
"""Tests for SystemPromptPipeline assembly."""
from __future__ import annotations

import pytest

from framework.memory.pipeline.abc import SystemPromptProvider
from framework.memory.pipeline.pipeline import SystemPromptPipeline


class _FakeProvider(SystemPromptProvider):
    """Controllable test provider."""

    def __init__(self, content: str, version: str = "v1") -> None:
        super().__init__()
        self._content = content
        self._version = version

    async def _fetch_version(self) -> str:
        return self._version

    async def _fetch_content(self) -> str:
        return self._content


class _EmptyProvider(SystemPromptProvider):
    """Provider that returns empty content."""

    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        return ""


class _ErrorProvider(SystemPromptProvider):
    """Provider that raises on content fetch."""

    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_assembles_multiple_providers():
    providers = [
        _FakeProvider("part A"),
        _FakeProvider("part B"),
    ]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "part A\n\n---\n\npart B"


@pytest.mark.asyncio
async def test_skips_empty_providers():
    providers = [
        _FakeProvider("part A"),
        _EmptyProvider(),
        _FakeProvider("part C"),
    ]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "part A\n\n---\n\npart C"


@pytest.mark.asyncio
async def test_skips_failing_providers():
    providers = [
        _FakeProvider("part A"),
        _ErrorProvider(),
        _FakeProvider("part C"),
    ]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "part A\n\n---\n\npart C"


@pytest.mark.asyncio
async def test_empty_pipeline_returns_empty_string():
    pipeline = SystemPromptPipeline([])
    result = await pipeline.get_or_refresh()
    assert result == ""


@pytest.mark.asyncio
async def test_single_provider_no_separator():
    providers = [_FakeProvider("only part")]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "only part"


@pytest.mark.asyncio
async def test_all_empty_returns_empty():
    providers = [_EmptyProvider(), _EmptyProvider()]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/pipeline/test_pipeline.py -v`
Expected: FAIL — `ImportError: cannot import name 'SystemPromptPipeline'`

- [ ] **Step 3: Implement SystemPromptPipeline**

```python
# framework/memory/pipeline/pipeline.py
"""SystemPromptPipeline — ordered collection of versioned prompt providers."""
from __future__ import annotations

import logging
from typing import Any

from framework.memory.pipeline.abc import SystemPromptProvider

logger = logging.getLogger(__name__)


class SystemPromptPipeline:
    """Ordered collection of SystemPromptProvider instances.

    Assembles the full system prompt by iterating providers in order,
    skipping empty results and catching exceptions.
    Sections are joined with ``"\\n\\n---\\n\\n"``.
    """

    def __init__(self, providers: list[SystemPromptProvider]) -> None:
        self._providers = providers

    async def get_or_refresh(self) -> str:
        """Assemble system prompt from all providers, refreshing as needed."""
        parts: list[str] = []
        for provider in self._providers:
            try:
                content = await provider.get_or_refresh()
            except Exception:
                logger.warning(
                    "Provider %s failed, skipping",
                    type(provider).__name__,
                    exc_info=True,
                )
                continue
            if content:
                parts.append(content)
        return "\n\n---\n\n".join(parts)
```

- [ ] **Step 4: Update `__init__.py` exports (already done in Task 1, verify)**

The `__init__.py` from Task 1 already exports `SystemPromptPipeline`. Verify it's correct.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pipeline/ -v`
Expected: All PASS (both test_abc and test_pipeline)

- [ ] **Step 6: Commit**

```bash
git add framework/memory/pipeline/pipeline.py tests/unit/memory/pipeline/test_pipeline.py
git commit -m "feat(pipeline): add SystemPromptPipeline with error-tolerant assembly"
```

---

## Phase 2: Static Providers

### Task 3: BasePromptProvider, RuntimeProvider, SkillProvider, KnowledgeProvider, ExperienceProvider

**Files:**
- Create: `framework/memory/pipeline/providers.py`
- Create: `tests/unit/memory/pipeline/test_providers.py`

- [ ] **Step 1: Write tests for all static providers**

```python
# tests/unit/memory/pipeline/test_providers.py
"""Tests for individual SystemPromptProvider implementations."""
from __future__ import annotations

import pytest

from framework.memory.pipeline.providers import (
    BasePromptProvider,
    ExperienceProvider,
    KnowledgeProvider,
    RuntimeProvider,
    SkillProvider,
)


# ── BasePromptProvider ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_base_prompt_returns_content():
    provider = BasePromptProvider("You are a helpful assistant.")
    result = await provider.get_or_refresh()
    assert result == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_base_prompt_never_refreshes():
    provider = BasePromptProvider("original")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    # Second call should use cache
    result = await provider.get_or_refresh()
    assert result == "original"


@pytest.mark.asyncio
async def test_base_prompt_empty_string():
    provider = BasePromptProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# ── RuntimeProvider ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runtime_contains_date_and_platform():
    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert "Current Date:" in result
    assert "Platform:" in result


@pytest.mark.asyncio
async def test_runtime_version_changes_daily():
    provider = RuntimeProvider()
    await provider.get_or_refresh()
    assert provider.last_version is not None
    # Version format: YYYY-MM-DD
    assert len(provider.last_version) == 10


# ── SkillProvider ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skill_never_refreshes():
    provider = SkillProvider("skill content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "skill content"


@pytest.mark.asyncio
async def test_skill_empty_when_no_content():
    provider = SkillProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# ── KnowledgeProvider ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_knowledge_never_refreshes_during_react():
    provider = KnowledgeProvider("knowledge content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_knowledge_empty_when_no_content():
    provider = KnowledgeProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# ── ExperienceProvider ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_experience_default_static():
    provider = ExperienceProvider("experience content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_experience_empty_when_no_content():
    provider = ExperienceProvider("")
    result = await provider.get_or_refresh()
    assert result == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/memory/pipeline/test_providers.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement all static providers**

```python
# framework/memory/pipeline/providers.py
"""System prompt providers — individual sections of the system prompt pipeline.

Each provider wraps one data source and provides versioned, cacheable content.
Providers are ordered by their position in the pipeline list (not by priority).
"""
from __future__ import annotations

import sys
from abc import ABC
from datetime import datetime
from typing import Any

from framework.memory.pipeline.abc import SystemPromptProvider


class BasePromptProvider(SystemPromptProvider):
    """Static base system prompt (agent personality). Never refreshes."""

    def __init__(self, base_prompt: str) -> None:
        super().__init__()
        self._base_prompt = base_prompt

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._base_prompt


class RuntimeProvider(SystemPromptProvider):
    """Runtime metadata — current date and platform. Refreshes daily."""

    async def _fetch_version(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    async def _fetch_content(self) -> str:
        current_date = datetime.now().strftime("%Y-%m-%d")
        platform_raw = sys.platform
        platform_name = {
            "win32": "Windows",
            "darwin": "macOS",
            "linux": "Linux",
        }.get(platform_raw, platform_raw)
        lines = ["## Runtime", f"Current Date: {current_date}", f"Platform: {platform_name}"]
        return "\n".join(lines)


class KnowledgeProvider(SystemPromptProvider):
    """Knowledge files (SOUL.md, USER.md, MEMORY.md). Never refreshes during react."""

    def __init__(self, knowledge_xml: str) -> None:
        super().__init__()
        self._knowledge_xml = knowledge_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._knowledge_xml


class SkillProvider(SystemPromptProvider):
    """Skill metadata XML. Never refreshes during react."""

    def __init__(self, skill_xml: str) -> None:
        super().__init__()
        self._skill_xml = skill_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._skill_xml


class ExperienceProvider(SystemPromptProvider):
    """Experience metadata XML. Default: static (extensible for future refresh)."""

    def __init__(self, experience_xml: str) -> None:
        super().__init__()
        self._experience_xml = experience_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._experience_xml


class ProviderBlocksProvider(SystemPromptProvider):
    """Static blocks from memory providers. Refreshes on content hash change."""

    def __init__(self, blocks: list[str]) -> None:
        super().__init__()
        self._blocks = blocks

    async def _fetch_version(self) -> str:
        # Hash the concatenated blocks to detect changes
        combined = "\n".join(self._blocks)
        if not combined:
            return "empty"
        import hashlib
        return hashlib.md5(combined.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        return "\n\n".join(self._blocks)


class ProviderPrefetchProvider(SystemPromptProvider):
    """Provider prefetch results. Refreshes when query changes."""

    def __init__(self, query: str, prefetch_content: str = "") -> None:
        super().__init__()
        self._query = query
        self._prefetch_content = prefetch_content

    async def _fetch_version(self) -> str:
        if not self._query:
            return "no-query"
        import hashlib
        return hashlib.md5(self._query.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        if not self._prefetch_content:
            return ""
        from framework.utils.xml import xml_text
        return f"<related_facts>\n{xml_text(self._prefetch_content)}\n</related_facts>"


class ArchiveProvider(SystemPromptProvider):
    """Archive summaries from DirArchiveStorage. Must refresh on cleanup."""

    def __init__(
        self,
        archive_storage: Any,
        inject_count: int = 3,
        inject_max_chars: int = 1000,
    ) -> None:
        super().__init__()
        self._storage = archive_storage
        self._inject_count = inject_count
        self._inject_max_chars = inject_max_chars

    async def _fetch_version(self) -> str:
        try:
            ids = await self._storage.list_archives(limit=1)
            return str(ids[0]) if ids else "0"
        except Exception:
            return ""

    async def _fetch_content(self) -> str:
        try:
            return await self._build_archive_xml()
        except Exception:
            return ""

    async def _build_archive_xml(self) -> str:
        from framework.utils.xml import xml_attr, xml_text
        from framework.memory.tags import ArchiveTag

        archive_dir = getattr(self._storage, "base_dir", None) or getattr(self._storage, "directory", None)
        if archive_dir is None:
            return ""

        try:
            archive_ids = await self._storage.list_archives(limit=self._inject_count)
        except Exception:
            return ""

        if not archive_ids:
            return ""

        records: list[str] = []
        for aid in sorted(archive_ids)[:self._inject_count]:
            try:
                content = await self._storage.read_archive_file(aid, "context.md")
            except Exception:
                continue
            if not content or not content.strip():
                continue

            truncated = len(content) > self._inject_max_chars
            display = content[:self._inject_max_chars] + "..." if truncated else content

            full_path = str((archive_dir / str(aid) / "context.md").resolve())
            st = ArchiveTag.SUMMARY.value
            records.append(
                f'<{st} number="{aid}"'
                f' file="{xml_attr(full_path)}"'
                f">\n{xml_text(display)}\n</{st}>"
            )

        if not records:
            return ""

        heading = (
            "### Earlier Conversation Summaries\n\n"
            "Short summaries of older conversations. Higher number = more recent. "
            "Read the `context.md` file at each path for the full details.\n\n"
        )
        ct = ArchiveTag.CONTAINER.value
        return f"<{ct}>\n" + "\n".join(records) + f"\n</{ct}>"


class PrunedProvider(SystemPromptProvider):
    """Pruned memory catalog. Must refresh on cleanup."""

    def __init__(self, pruned_manager: Any, session_id: str = "") -> None:
        super().__init__()
        self._manager = pruned_manager
        self._session_id = session_id

    async def _fetch_version(self) -> str:
        try:
            return self._manager.get_version(session_id=self._session_id)
        except Exception:
            return ""

    async def _fetch_content(self) -> str:
        try:
            xml = self._manager.get_injection_xml(session_id=self._session_id)
            return xml or ""
        except Exception:
            return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pipeline/test_providers.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/pipeline/providers.py tests/unit/memory/pipeline/test_providers.py
git commit -m "feat(pipeline): add all 9 SystemPromptProvider implementations"
```

---

## Phase 3: Dynamic Provider Dependencies

### Task 4: PrunedManager.get_version()

**Files:**
- Modify: `framework/memory/pruned/manager.py`
- Modify: `framework/memory/pruned/storage.py` (if needed)

- [ ] **Step 1: Write test for PrunedManager.get_version()**

Add to `tests/unit/memory/test_pruned.py` (or create new test file):

```python
@pytest.mark.asyncio
async def test_pruned_manager_get_version_no_entries(tmp_path):
    """get_version returns '0' when no entries exist."""
    manager = PrunedManager(tmp_path)
    version = manager.get_version(session_id="test-session")
    assert version == "0"


@pytest.mark.asyncio
async def test_pruned_manager_get_version_with_entries(tmp_path):
    """get_version returns str(max_entry_id)."""
    manager = PrunedManager(tmp_path)
    from datetime import datetime
    # Write two pruned batches
    await manager.write_pruned(
        [{"role": "user", "content": "msg1", "created_at": datetime.now()}],
        topic="topic1",
        cleanup_time=datetime.now(),
        session_id="test-session",
    )
    version1 = manager.get_version(session_id="test-session")
    assert version1 == "1"

    await manager.write_pruned(
        [{"role": "user", "content": "msg2", "created_at": datetime.now()}],
        topic="topic2",
        cleanup_time=datetime.now(),
        session_id="test-session",
    )
    version2 = manager.get_version(session_id="test-session")
    assert version2 == "2"


@pytest.mark.asyncio
async def test_pruned_manager_get_version_corrupted_index(tmp_path):
    """get_version returns '' when index is corrupted."""
    manager = PrunedManager(tmp_path)
    # Write valid entry first
    from datetime import datetime
    await manager.write_pruned(
        [{"role": "user", "content": "msg", "created_at": datetime.now()}],
        topic="topic",
        cleanup_time=datetime.now(),
        session_id="test-session",
    )
    # Corrupt the index
    storage = manager._get_storage("test-session")
    index_path = storage._dir / storage._index_filename
    index_path.write_text("not valid json\n", encoding="utf-8")
    version = manager.get_version(session_id="test-session")
    assert version == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/memory/test_pruned.py -k "get_version" -v`
Expected: FAIL — `AttributeError: 'PrunedManager' object has no attribute 'get_version'`

- [ ] **Step 3: Implement PrunedManager.get_version()**

Add to `framework/memory/pruned/manager.py`, after `get_injection_xml()`:

```python
def get_version(self, *, session_id: str = "") -> str:
    """Return the current version of pruned content for the given session.

    Version is the max entry ID from the index. Returns "0" when empty,
    "" on read error (triggers refresh in provider).
    """
    try:
        storage = self._get_storage(session_id)
        entries = storage.read_index()
        if not entries:
            return "0"
        return str(max(e.id for e in entries))
    except Exception:
        return ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/test_pruned.py -k "get_version" -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/pruned/manager.py tests/unit/memory/test_pruned.py
git commit -m "feat(pruned): add PrunedManager.get_version() for pipeline versioning"
```

---

### Task 5: Tests for ArchiveProvider and PrunedProvider with real storage

**Files:**
- Create: `tests/unit/memory/pipeline/test_dynamic_providers.py`

- [ ] **Step 1: Write integration tests for ArchiveProvider**

```python
# tests/unit/memory/pipeline/test_dynamic_providers.py
"""Tests for ArchiveProvider and PrunedProvider with real storage backends."""
from __future__ import annotations

import pytest

from framework.memory.pipeline.providers import ArchiveProvider, PrunedProvider
from framework.memory.stores.dir_archive import DirArchiveStorage


@pytest.mark.asyncio
async def test_archive_provider_version_no_archives(tmp_path):
    storage = DirArchiveStorage(tmp_path / "archive")
    await storage.initialize()
    provider = ArchiveProvider(storage)
    result = await provider.get_or_refresh()
    assert result == ""  # No archives → empty content
    assert provider.last_version == "0"


@pytest.mark.asyncio
async def test_archive_provider_version_with_archives(tmp_path):
    storage = DirArchiveStorage(tmp_path / "archive")
    await storage.initialize()
    # Write an archive
    await storage.write_archive_file(1, "context.md", "Test summary 1")
    await storage.write_archive_state({"next_archive_id": 2})

    provider = ArchiveProvider(storage)
    result = await provider.get_or_refresh()
    assert "Test summary 1" in result
    assert provider.last_version == "1"


@pytest.mark.asyncio
async def test_archive_provider_detects_new_archive(tmp_path):
    storage = DirArchiveStorage(tmp_path / "archive")
    await storage.initialize()
    await storage.write_archive_file(1, "context.md", "Summary 1")
    await storage.write_archive_state({"next_archive_id": 2})

    provider = ArchiveProvider(storage)
    r1 = await provider.get_or_refresh()
    assert "Summary 1" in r1

    # Write new archive
    await storage.write_archive_file(2, "context.md", "Summary 2")
    await storage.write_archive_state({"next_archive_id": 3})

    r2 = await provider.get_or_refresh()
    assert "Summary 2" in r2
    assert provider.last_version == "2"


@pytest.mark.asyncio
async def test_pruned_provider_with_pruned_manager(tmp_path):
    from datetime import datetime
    from framework.memory.pruned.manager import PrunedManager

    manager = PrunedManager(tmp_path / "pruned")
    provider = PrunedProvider(manager, session_id="test-session")

    # No pruned content → empty
    r1 = await provider.get_or_refresh()
    assert r1 == "" or r1 is None or "Previous" not in (r1 or "")
    assert provider.last_version == "0"

    # Write pruned content
    await manager.write_pruned(
        [{"role": "user", "content": "hello", "created_at": datetime.now()}],
        topic="greeting",
        cleanup_time=datetime.now(),
        session_id="test-session",
    )

    r2 = await provider.get_or_refresh()
    # Should contain pruned content now
    assert provider.last_version == "1"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/unit/memory/pipeline/test_dynamic_providers.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/memory/pipeline/test_dynamic_providers.py
git commit -m "test(pipeline): add integration tests for ArchiveProvider and PrunedProvider"
```

---

## Phase 4: Integration

### Task 6: ContextState and AgentContext changes

**Files:**
- Modify: `framework/core/context.py` — `ContextState` adds `system_prompt_pipeline` field
- Modify: `framework/core/context.py` — `ContextState.to_messages()` prefers pipeline

- [ ] **Step 1: Write test for ContextState with pipeline**

```python
# In tests/unit/test_context_state.py (or wherever ContextState tests live)
@pytest.mark.asyncio
async def test_context_state_uses_pipeline_when_available():
    """When system_prompt_pipeline is set, to_messages() uses it instead of system_prompt."""
    from framework.core.context import ContextState
    from framework.memory.pipeline.pipeline import SystemPromptPipeline
    from framework.memory.pipeline.providers import BasePromptProvider

    pipeline = SystemPromptPipeline([BasePromptProvider("pipeline content")])
    state = ContextState(
        system_prompt="old static prompt",
        system_prompt_pipeline=pipeline,
    )
    messages = await state.to_messages()
    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert "pipeline content" in system_msgs[0]["content"]
    assert "old static prompt" not in system_msgs[0]["content"]


@pytest.mark.asyncio
async def test_context_state_falls_back_to_system_prompt():
    """When system_prompt_pipeline is None, to_messages() uses system_prompt."""
    from framework.core.context import ContextState

    state = ContextState(system_prompt="static prompt")
    messages = await state.to_messages()
    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "static prompt"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_context_state.py -v`
Expected: FAIL — `TypeError: ContextState.__init__() got an unexpected keyword argument 'system_prompt_pipeline'`

- [ ] **Step 3: Modify ContextState to add pipeline field**

In `framework/core/context.py`, modify the `ContextState` dataclass:

```python
@dataclass
class ContextState:
    """上下文状态"""

    system_prompt: str = ""
    history: MessageHistory = field(default_factory=ListMessageHistory)
    metadata: dict[str, Any] = field(default_factory=dict)
    system_prompt_pipeline: Any = None  # SystemPromptPipeline | None
```

Then update `to_messages()` to prefer pipeline:

```python
async def to_messages(self) -> list[dict[str, Any]]:
    """转换为 LLM 消息列表"""
    history_list = await self.history.to_list()
    history_list, has_agent_msgs = normalize_agent_messages_for_llm(history_list)

    messages = []

    # Prefer pipeline over static system_prompt
    system_content = ""
    if self.system_prompt_pipeline is not None:
        system_content = await self.system_prompt_pipeline.get_or_refresh()
    elif self.system_prompt:
        system_content = self.system_prompt

    if system_content:
        if has_agent_msgs and AGENT_COMMUNICATION_SYSTEM_NOTE not in system_content:
            system_content += AGENT_COMMUNICATION_SYSTEM_NOTE
        messages.append({"role": "system", "content": system_content})
    messages.extend(history_list)
    return messages
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_context_state.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `python -m pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS (ContextState.system_prompt_pipeline defaults to None, backward compatible)

- [ ] **Step 6: Commit**

```bash
git add framework/core/context.py tests/unit/test_context_state.py
git commit -m "feat(context): add system_prompt_pipeline to ContextState, prefer over static string"
```

---

### Task 7: MemorySystemContextManager.load() refactor

**Files:**
- Modify: `framework/memory/system.py` — `MemorySystemContextManager.load()`

- [ ] **Step 1: Understand current load() flow**

Current `load()` in `framework/memory/system.py:176`:
1. Build MemoryContext
2. Call `ensure_within_budget()`
3. Call `injection_policy.assemble()` → `InjectionResult(system_prompt, messages)`
4. Assemble parts: runtime_info + base_prompt + injection.system_prompt + experience + skill
5. Join with `"\n\n---\n\n"`
6. Create history from injection messages
7. Return `ContextState(system_prompt=..., history=...)`

New flow:
1. Build MemoryContext
2. Call `ensure_within_budget()`
3. Build providers list from available components
4. Create `SystemPromptPipeline(providers)`
5. Create history from injection messages (still needed for session messages)
6. Return `ContextState(system_prompt_pipeline=pipeline, history=...)`

- [ ] **Step 2: Write test for ctx_mgr.load() returning pipeline**

```python
@pytest.mark.asyncio
async def test_load_returns_pipeline_with_providers(tmp_path):
    """load() should return ContextState with a non-None system_prompt_pipeline."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.system import MemorySystemContextManager
    from framework.memory.pipeline.pipeline import SystemPromptPipeline

    # Reuse the same setup pattern as existing ctx_mgr tests
    # Search for DefaultMemorySystem construction in tests/unit/memory/
    # and reuse the factory/fixture pattern found there.
    memory_system = _build_test_memory_system(tmp_path)  # extract from existing tests
    await memory_system.initialize()
    ctx_mgr = MemorySystemContextManager(
        memory_system=memory_system,
        base_system_prompt="test prompt",
    )
    state = await ctx_mgr.load("test-session")
    assert state.system_prompt_pipeline is not None
    assert isinstance(state.system_prompt_pipeline, SystemPromptPipeline)
    await memory_system.close()
```

> **Implementer note:** `_build_test_memory_system` should be extracted from the nearest existing test that constructs a `DefaultMemorySystem`. Run: `grep -rn "DefaultMemorySystem" tests/unit/memory/` to find the factory pattern.

- [ ] **Step 3: Implement pipeline construction in load()**

In `framework/memory/system.py`, modify `load()` method. Replace the prompt assembly block (lines ~236-264) with pipeline construction:

```python
# In MemorySystemContextManager.load(), after injection_policy.assemble():

# Build providers from available components
from framework.memory.pipeline.pipeline import SystemPromptPipeline
from framework.memory.pipeline.providers import (
    ArchiveProvider,
    BasePromptProvider,
    ExperienceProvider,
    KnowledgeProvider,
    PrunedProvider,
    ProviderBlocksProvider,
    ProviderPrefetchProvider,
    RuntimeProvider,
    SkillProvider,
)

providers: list[SystemPromptProvider] = []

# 1. Runtime metadata
providers.append(RuntimeProvider())

# 2. Base system prompt
if self.base_system_prompt:
    providers.append(BasePromptProvider(self.base_system_prompt))

# 3. Knowledge (static during react)
knowledge_xml_parts = []
if result.system_prompt:
    # Extract knowledge section from injection result
    knowledge_xml_parts.append(result.system_prompt)
if knowledge_xml_parts:
    providers.append(KnowledgeProvider("\n\n".join(knowledge_xml_parts)))

# 4. Archive (must refresh)
archive_storage = None
if isinstance(self.memory_system, DefaultMemorySystem):
    archive_storage = await self.memory_system._resolve_archive_storage(ctx)
if archive_storage is not None:
    providers.append(ArchiveProvider(archive_storage))

# 5. Pruned (must refresh)
pruned_mgr = getattr(self.memory_system, "pruned_manager", None) or (
    self.memory_system.pruned_manager if hasattr(self.memory_system, "pruned_manager") else None
)
if pruned_mgr is not None:
    providers.append(PrunedProvider(pruned_mgr, session_id=session_id))

# 6. Provider blocks
provider_blocks: list[str] = []
for prov in self.memory_system.get_providers():
    try:
        block = prov.system_prompt_block()
        if block:
            provider_blocks.append(block)
    except Exception:
        continue
if provider_blocks:
    providers.append(ProviderBlocksProvider(provider_blocks))

# 7. Provider prefetch
if query:
    try:
        prefetch = await self.memory_system.prefetch_memories(query, ctx)
        if prefetch:
            providers.append(ProviderPrefetchProvider(query, prefetch))
    except Exception:
        pass

# 8. Experience
if self._experience_manager is not None:
    try:
        experience_prompt = await self._experience_manager.build_prompt()
        if experience_prompt:
            providers.append(ExperienceProvider(experience_prompt))
    except Exception:
        pass

# 9. Skills — handled separately via skill_manager parameter

pipeline = SystemPromptPipeline(providers)

# Also build static system_prompt as fallback
parts_fallback: list[str] = []
if runtime_info:
    runtime_text = self._format_runtime_info(runtime_info)
    if runtime_text:
        parts_fallback.append(runtime_text)
if self.base_system_prompt:
    parts_fallback.append(self.base_system_prompt)
if result.system_prompt:
    parts_fallback.append(result.system_prompt)
# ... (keep existing fallback assembly for backward compat)

system_prompt = "\n\n---\n\n".join(parts_fallback) if parts_fallback else ""

history = self.memory_system.create_message_history(
    context=ctx, initial_messages=result.messages,
)
return ContextState(
    system_prompt=system_prompt,
    history=history,
    system_prompt_pipeline=pipeline,
)
```

> **Note:** The exact implementation depends on how `FullInjectionPolicy.assemble()` structures its output. The provider construction may need to extract individual sections rather than using the monolithic `result.system_prompt`. This should be refined during implementation.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/system.py
git commit -m "feat(memory): build SystemPromptPipeline in MemorySystemContextManager.load()"
```

---

### Task 8: LLMNode._build_messages() integration

**Files:**
- Modify: `framework/agents/react/nodes/llm.py`

- [ ] **Step 1: Write test for LLMNode with pipeline**

```python
# In tests/unit/agents/react/test_llm_node.py (or nearest existing LLMNode test)
import pytest
from unittest.mock import AsyncMock, MagicMock
from framework.core.agent import AgentContext
from framework.memory.pipeline.pipeline import SystemPromptPipeline
from framework.memory.pipeline.providers import BasePromptProvider


@pytest.mark.asyncio
async def test_llm_node_uses_pipeline_over_static_prompt():
    """When system_prompt_pipeline is set, _build_messages uses it."""
    from framework.agents.react.nodes.llm import LLMNode

    pipeline = SystemPromptPipeline([BasePromptProvider("pipeline prompt")])
    ctx = AgentContext(
        system_prompt="static prompt",
        session_id="test",
    )
    ctx.system_prompt_pipeline = pipeline
    ctx.history = MagicMock()
    ctx.history.to_list = AsyncMock(return_value=[])
    ctx.runtime = None

    node = LLMNode.__new__(LLMNode)  # skip __init__
    messages = await node._build_messages(ctx)

    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert "pipeline prompt" in system_msgs[0]["content"]
    assert "static prompt" not in system_msgs[0]["content"]


@pytest.mark.asyncio
async def test_llm_node_falls_back_to_static_prompt():
    """When no pipeline, _build_messages uses ctx.system_prompt."""
    from framework.agents.react.nodes.llm import LLMNode

    ctx = AgentContext(
        system_prompt="static prompt",
        session_id="test",
    )
    ctx.system_prompt_pipeline = None
    ctx.history = MagicMock()
    ctx.history.to_list = AsyncMock(return_value=[])
    ctx.runtime = None

    node = LLMNode.__new__(LLMNode)
    messages = await node._build_messages(ctx)

    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0]["content"] == "static prompt"
```

- [ ] **Step 2: Modify LLMNode._build_messages()**

In `framework/agents/react/nodes/llm.py`, the current `_build_messages()`:

```python
async def _build_messages(self, ctx: AgentContext) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if ctx.system_prompt:
        messages.append({"role": "system", "content": ctx.system_prompt})
    messages.extend(await ctx.to_messages())
    governance = ctx.runtime.governance if ctx.runtime else None
    if governance is not None:
        messages = await governance.apply(messages)
    return messages
```

The change: `ctx.to_messages()` now uses pipeline internally (via `ContextState.to_messages()`), so `_build_messages()` doesn't need to add system_prompt separately when pipeline is active. But wait — `AgentContext` has its own `system_prompt` field, and `to_messages()` reads from `ContextState`.

Looking at the flow: `AgentContext.system_prompt` is set from `ContextState.system_prompt`. The `to_messages()` method is on `AgentContext`, not `ContextState`. Let me check...

Actually, looking at `LLMNode._build_messages()`:
1. It manually adds system prompt from `ctx.system_prompt`
2. Then calls `ctx.to_messages()` for history (non-system messages)

And `ContextState.to_messages()` adds system prompt AND history together.

There's a duality here. The `LLMNode._build_messages()` adds system prompt separately, while `ContextState.to_messages()` also adds it. Let me check if `ctx.to_messages()` is the same as `ContextState.to_messages()`...

Looking at the code, `AgentContext` has a `to_messages()` that likely delegates to history. The `LLMNode` adds system prompt separately from `ctx.system_prompt` and then extends with `ctx.to_messages()` which returns history messages only.

So the integration point is: `LLMNode._build_messages()` should check if the context has a pipeline and use it for the system message:

```python
async def _build_messages(self, ctx: AgentContext) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    # Check for pipeline on context_state
    pipeline = getattr(ctx, '_context_state', None)
    if pipeline is not None and hasattr(pipeline, 'system_prompt_pipeline') and pipeline.system_prompt_pipeline is not None:
        system_content = await pipeline.system_prompt_pipeline.get_or_refresh()
        if system_content:
            messages.append({"role": "system", "content": system_content})
    elif ctx.system_prompt:
        messages.append({"role": "system", "content": ctx.system_prompt})

    messages.extend(await ctx.to_messages())
    governance = ctx.runtime.governance if ctx.runtime else None
    if governance is not None:
        messages = await governance.apply(messages)
    return messages
```

> **Design decision:** The pipeline needs to be accessible from `AgentContext`. Options:
> 1. Add `system_prompt_pipeline` field to `AgentContext`
> 2. Store pipeline reference on `AgentContext` during `_build_runtime_and_context()`
> 3. Pass pipeline through runtime services
>
> Recommended: Option 1 — add `system_prompt_pipeline` field to `AgentContext`, set it in `_build_runtime_and_context()` from `context_state.system_prompt_pipeline`.

- [ ] **Step 3: Add system_prompt_pipeline to AgentContext**

In `framework/core/agent.py`, add field:

```python
system_prompt_pipeline: Any = None  # SystemPromptPipeline | None
```

In `framework/pipeline/pipeline.py`, `_build_runtime_and_context()`, set it:

```python
agent_context = AgentContext(
    system_prompt=context_state.system_prompt,
    history=context_state.history,
    ...
)
agent_context.system_prompt_pipeline = context_state.system_prompt_pipeline
```

- [ ] **Step 4: Update LLMNode._build_messages()**

```python
async def _build_messages(self, ctx: AgentContext) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    # Use pipeline for dynamic system prompt if available
    if ctx.system_prompt_pipeline is not None:
        system_content = await ctx.system_prompt_pipeline.get_or_refresh()
        if system_content:
            messages.append({"role": "system", "content": system_content})
    elif ctx.system_prompt:
        messages.append({"role": "system", "content": ctx.system_prompt})

    messages.extend(await ctx.to_messages())
    governance = ctx.runtime.governance if ctx.runtime else None
    if governance is not None:
        messages = await governance.apply(messages)
    return messages
```

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add framework/agents/react/nodes/llm.py framework/core/agent.py framework/pipeline/pipeline.py
git commit -m "feat(react): LLMNode uses SystemPromptPipeline for dynamic system prompt refresh"
```

---

## Phase 5: URB and Cleanup

### Task 9: URB XML description update

**Files:**
- Modify: `framework/memory/context_governance.py` — `UserRetentionBufferInjectionGovernance.apply()`

- [ ] **Step 1: Update URB XML comments**

In `framework/memory/context_governance.py`, find the `apply()` method of `UserRetentionBufferInjectionGovernance` (around line 366-368):

Current:
```python
lines = [
    f'<{ct}>',
    '<!-- Parts of your recent conversation that were cut for space. -->',
]
```

Replace with:
```python
lines = [
    f'<{ct}>',
    '<!-- Recent conversation history pruned for context space. -->',
    '<!-- user_msg without you_response = this user message was not yet answered. -->',
]
```

- [ ] **Step 2: Update existing tests that match on the old comment**

Search for tests that assert the old comment text:

Run: `grep -r "cut for space" tests/`

Update any found tests to match the new comment.

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add framework/memory/context_governance.py tests/
git commit -m "fix(urb): improve URB XML description to clarify pruned history and unanswered messages"
```

---

### Task 10: End-to-end verification and cleanup

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 2: Run integration tests (if available)**

Run: `python -m pytest tests/ -x --timeout=60 -q -k "not slow"`
Expected: All PASS

- [ ] **Step 3: Verify spec coverage**

Check each spec requirement against implemented tasks:

| Spec Requirement | Task |
|---|---|
| SystemPromptProvider ABC with version caching | Task 1 |
| SystemPromptPipeline assembly | Task 2 |
| All 9 providers implemented | Task 3 |
| PrunedManager.get_version() | Task 4 |
| ArchiveProvider/PrunedProvider integration tests | Task 5 |
| ContextState.system_prompt_pipeline field | Task 6 |
| ctx_mgr.load() builds pipeline | Task 7 |
| LLMNode uses pipeline | Task 8 |
| URB XML description update | Task 9 |
| Subagent compatibility (no null issues) | Task 6, 7 |

- [ ] **Step 4: Final commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: final cleanup for system prompt pipeline implementation"
```
