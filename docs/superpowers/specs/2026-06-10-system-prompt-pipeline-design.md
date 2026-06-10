# System Prompt Pipeline Design

> Date: 2026-06-10
> Status: Approved
> Scope: framework/memory, framework/pipeline, framework/agents/react

## Problem

During a ReAct turn, the system prompt is assembled once in `MemorySystemContextManager.load()` and frozen as a `str` on `ContextState`. If memory cleanup/compression runs mid-turn (or between turns with long-lived objects), the system prompt becomes stale — archive summaries and pruned catalogs are not refreshed.

Additionally, each memory layer (knowledge, archive, pruned, experience, skills) is tightly coupled inside `FullInjectionPolicy._inject_*()` methods. There is no way to independently refresh individual sections.

## Design Goals

1. **Selective refresh**: Archive and pruned sections must update within a turn when cleanup/compression occurs.
2. **Extensible**: Other sections (knowledge, experience) should be refreshable in the future without architectural changes.
3. **Default-safe**: Knowledge and skills should NOT refresh during react by default.
4. **Subagent-compatible**: Missing layers (no archive, no knowledge) must work without null checks.
5. **Minimal I/O**: Version-based caching avoids unnecessary re-reads across iterations within a turn.

## Architecture

### SystemPromptProvider ABC

Each system prompt section is an independent provider with internal version-based caching.

```python
class SystemPromptProvider(ABC):
    """One section of the system prompt pipeline with version-based caching."""

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
        """Get fresh content from underlying storage."""

    async def get_or_refresh(self) -> str:
        """Return cached content or refresh if version changed."""
        current = await self._fetch_version()
        if self._last_version is None or current != self._last_version:
            self._cached_content = await self._fetch_content()
            self._last_version = current
        return self._cached_content

    @property
    def last_version(self) -> str | None:
        """For debugging/logging."""
        return self._last_version
```

**Key behaviors:**
- `_last_version is None` (initial state) → always fetches on first call
- Version mismatch → re-fetches content and updates cache
- Version match → returns cached content, zero I/O
- `_fetch_version()` returns `""` on error → forces refresh

### SystemPromptPipeline

Holds an ordered list of providers and assembles the full system prompt.

```python
class SystemPromptPipeline:
    """Ordered collection of SystemPromptProvider instances."""

    def __init__(self, providers: list[SystemPromptProvider]) -> None:
        self._providers = providers

    async def get_or_refresh(self) -> str:
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

**Key behaviors:**
- Providers that return empty string are skipped
- Provider exceptions are caught and logged, not propagated
- Missing layers (subagent scenario) are simply not in the list

## Providers (9 total)

| # | Provider | Version Source | Refresh Policy |
|---|---|---|---|
| 1 | RuntimeProvider | `datetime.now().strftime("%Y-%m-%d")` | Auto (daily) |
| 2 | BasePromptProvider | `"static"` | Never |
| 3 | KnowledgeProvider | `"static"` | **Never during react** |
| 4 | ArchiveProvider | `str(max_archive_id)` from `list_archives(limit=1)` | **Must refresh** |
| 5 | PrunedProvider | `str(max_entry_id)` from `read_index()` | **Must refresh** |
| 6 | ProviderBlocksProvider | hash[:16] of concatenated blocks | Default no |
| 7 | ProviderPrefetchProvider | hash[:16] of query string | On query change |
| 8 | ExperienceProvider | `"static"` | Extensible future |
| 9 | SkillProvider | `"static"` | **Never** |

### Version Source Details

#### ArchiveProvider
- **Storage**: `DirArchiveStorage` with numbered directories (1/, 2/, 3/, ...)
- **Version**: `str(max_id)` from `list_archives(limit=1)`
- **No archives**: returns `"0"`
- **Cleanup writes new archive** → max_id increments → version changes

#### PrunedProvider
- **Storage**: `FilePrunedStorage` with `index.jsonl` containing `PrunedIndexEntry` with monotonic `id`
- **Version**: `str(max(entry.id))` from `read_index()`
- **No entries**: returns `"0"`
- **Cleanup writes new pruned** → max_id increments → version changes

#### KnowledgeProvider
- **Version**: `"static"` — does NOT refresh during react even if files change
- **Rationale**: Mid-turn personality/facts changes would confuse the LLM
- **Note**: `KnowledgeManager.get_version()` may still be added for non-react use cases

#### ExperienceProvider
- **Version**: `"static"` — default implementation does not refresh
- **Extensibility**: Future implementations can use `ExperienceManager.get_version()` → count + max_mtime

#### SkillProvider
- **Version**: `"static"` — never refreshes during react

## Lifecycle

### Per-turn reconstruction

Providers are **reconstructed** each turn in `ctx_mgr.load()`. Physical storage is not rebuilt — only the Python objects.

```
Turn 1 enters
  → ctx_mgr.load() → providers constructed → _last_version = None

  Iteration 1: get_or_refresh()
    → _last_version is None → force fetch → cache version + content

  Iteration 2..N: get_or_refresh()
    → version unchanged → cache hit, zero I/O

Turn 1 ends → cleanup may run → archive/pruned data changes on disk

Turn 2 enters
  → ctx_mgr.load() → providers reconstructed → _last_version = None

  Iteration 1: get_or_refresh()
    → _last_version is None → force fetch → gets updated data ✓
```

### Integration points

1. **`MemorySystemContextManager`** holds `_pipeline: SystemPromptPipeline` and reconstructs providers in `load()`
2. **`ContextState`** changes from `system_prompt: str` to `system_prompt_pipeline: SystemPromptPipeline` (reference, not ownership)
3. **`LLMNode._build_messages()`** calls `await ctx.system_prompt_pipeline.get_or_refresh()` instead of reading frozen `ctx.system_prompt`
4. **`FullInjectionPolicy`** is decomposed into individual providers; `RestrictedInjectionPolicy` is preserved for subagents

### Subagent scenario

Subagents use a minimal pipeline with only the providers they have:

```python
# Subagent pipeline — missing layers simply not included
providers = [
    RuntimeProvider(),
    BasePromptProvider(subagent_prompt),
    SkillProvider(skill_manager),  # optional
]
```

`RestrictedInjectionPolicy` continues to handle session messages (history layer, not system prompt).

## Methods to Add

| Layer | Method | Purpose |
|---|---|---|
| `PrunedManager` | `get_version(session_id) -> str` | Encapsulate max entry id lookup |
| `KnowledgeManager` | `get_version() -> str` | Future: mtime-based version for non-react use |
| `ExperienceManager` | `get_version() -> str` | Future: count + max_mtime version |

## URB Improvements (Independent)

The User Retention Buffer stays in the **governance layer** (`UserRetentionBufferInjectionGovernance`), NOT in the system prompt pipeline. It already refreshes every iteration via `get_entries()`.

**XML description improvement**: clarify that URB contains pruned conversation history and indicate unanswered user messages.

Current:
```xml
<!-- Parts of your recent conversation that were cut for space. -->
```

Proposed:
```xml
<!-- Recent conversation history pruned for context space.
     user_msg without you_response = this user message was not yet answered. -->
```

## Migration Path

1. Create `SystemPromptProvider` ABC and `SystemPromptPipeline`
2. Implement all 9 providers, migrating logic from `FullInjectionPolicy._inject_*()` methods
3. Modify `MemorySystemContextManager.load()` to build pipeline and return `ContextState` with pipeline reference
4. Modify `LLMNode._build_messages()` to use `pipeline.get_or_refresh()`
5. Add `get_version()` to `PrunedManager` (required), `KnowledgeManager` (optional), `ExperienceManager` (optional)
6. Keep `FullInjectionPolicy` temporarily for backwards compatibility, remove after migration
7. Update URB XML description
8. Preserve `RestrictedInjectionPolicy` for subagents

## Error Handling

- `_fetch_version()` returns `""` on any error → forces refresh
- `_fetch_content()` returns `""` on any error → section skipped in pipeline
- `SystemPromptPipeline.get_or_refresh()` catches provider exceptions → logs and skips
- JSON parse failures in `read_index()` / `read_archive_state()` → already handled by existing robust readers, version returns `"0"` or `""`
