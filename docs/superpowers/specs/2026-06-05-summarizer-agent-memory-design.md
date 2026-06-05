# Summarizer Agent Memory System Redesign

**Date:** 2026-06-05
**Status:** Draft

## Overview

Redesign the memory system's archive and knowledge layers so that:

1. **SummarizerAgent becomes a real agent** — uses ReAct engine + scoped file tools to generate archive files (context.md, knowledge.md, index.md) instead of single-shot LLM calls writing to JSONL.
2. **DreamEngine becomes a real agent** — uses ReAct engine + scoped file tools to read archive knowledge.md files and update knowledge files (SOUL.md, USER.md, MEMORY.md).
3. **Archive storage changes from JSONL to MD directory model** — each archive is a directory (`archive/{id}/`) containing three MD files.
4. **Cleanup ordering guarantees** archive generation → pruned index refresh → session cleanup → archive_id commit.

## Design Decisions

| Decision | Choice |
|----------|--------|
| Archive directory structure | Three files: `context.md`, `knowledge.md`, `index.md` |
| SummarizerAgent | Reuse ReAct engine, no hooks/interceptors, scoped file tools |
| DreamEngine | Also becomes real agent with scoped file tools |
| invocation_id | Equals archive_id (auto-increment integer) |
| Pruned relationship | index.md is data source for PrunedManager; PrunedManager still handles injection |
| Cleanup ordering | archive → pruned index refresh → session cleanup → archive_id commit |
| Idempotency | On retry, check directory completeness; skip if intact, overwrite if partial |
| Storage format | MD files replace JSONL for all consumption |
| Tool scoping | System prompt declares allowed dirs + tools validate paths at runtime |
| Knowledge injection | Unchanged — no truncation changes, only update method changes |

## Section 1: Archive Storage Model

### Current → Target

```
Current: JSONL per channel
  data/archive/{scope}/
    context_archive.jsonl
    knowledge_archive.jsonl
    .archive_state

Target: Directory per archive_id with MD files
  data/archive/{scope}/
    state.json               ← {next_archive_id, knowledge_consumed_archive_id, ...}
    1/
      context.md             ← context channel summary
      knowledge.md           ← knowledge channel fact extraction
      index.md               ← pruned index (ultra-concise)
    2/
      context.md
      knowledge.md
      index.md
```

### ArchiveStorage Protocol

```python
class ArchiveStorage(Protocol):
    """MD-based archive storage abstraction."""

    async def ensure_archive_dir(self, archive_id: int) -> Path: ...
    async def read_archive_file(self, archive_id: int, filename: str) -> str | None: ...
    async def write_archive_file(self, archive_id: int, filename: str, content: str) -> None: ...
    async def list_archives(self, since_id: int = 0, limit: int = 100) -> list[int]: ...
    async def read_state(self) -> ArchiveState: ...
    async def write_state(self, state: ArchiveState) -> None: ...
    async def is_archive_complete(self, archive_id: int) -> bool: ...
```

All injection, DreamEngine consumption, and cleanup code depend on this protocol — never read MD files directly.

### Backward Compatibility

- Old `.archive_state` format migrated to `state.json` on initialization.
- Old JSONL files are not migrated; only new archives use MD directories.
- Transition: `cleanup_session` checks `archive_agent` parameter; if present, new path; otherwise old strategy.

## Section 1.5: Summarizer Isolated Memory

### Principle

The summarizer agent has its **own isolated session storage**, completely separate from the multi-level memory system (archive/knowledge/session). This memory is:

- **Not stored inside** the archive, knowledge, or session layers
- **Only accessible by the summarizer itself** (via scoped tools)
- **Session-level only** — no archive/knowledge hierarchy for the summarizer
- **Keyed by invocation_id** (archive_id) for each invocation

### Storage Layout

```
{data_dir}/summarizer/{archive_id}/
  session.jsonl        ← ReAct session log (tool calls + LLM responses)
```

This path is derived from `data_dir` (same as `.modex/` or `MODEX_DATA_DIR`), NOT nested under `memory/`. The summarizer's memory area is a peer of `memory/`, `runtime_state/`, `inbox/`, etc.

### Why Separate

1. **No circular dependency**: The summarizer manages the memory system; it should not store its working data inside the system it manages.
2. **Isolation**: The summarizer's session data is for its own use (debugging, auditing, potential resumption), not for the main agent or other subsystems.
3. **Workspace switching**: On `cd`/`exit`, the `data_dir` changes, and the summarizer naturally gets a new isolated area — same mechanism as all other storage.

### Scoped Tool Access

The summarizer's scoped tools do **NOT** include the session directory. Session data is framework-managed only.

**ArchiveSummarizer tools** only access:
- `archive/{scope}/{archive_id}/` — to write context.md, knowledge.md, index.md

**KnowledgeConsolidator tools** only access:
- `archive/{id}/` (read-only) — to read knowledge.md files
- `knowledge/` (read-write) — to update SOUL.md, USER.md, MEMORY.md

The agent cannot read or write its own session data. The session.jsonl is written by framework code wrapping the agent execution — it records tool calls and LLM responses for debugging/auditing, but the agent is unaware of it.

### KnowledgeConsolidator Session

Same pattern: `{data_dir}/consolidator/{cursor}/session.jsonl` — framework-managed, isolated from the memory system, keyed by dream cursor position. Agent cannot access it.

## Section 2: ScopedFileTools

Four tools for MemoryAgent, each constructed with `allowed_dirs: list[Path]`:

| Tool | Capability |
|------|-----------|
| `ScopedReadFileTool` | Read file content |
| `ScopedWriteFileTool` | Write/create file |
| `ScopedEditFileTool` | String replacement edit |
| `ScopedListTool` | List directory contents |

### Path Validation

```python
def _validate_path(self, raw_path: str) -> Path:
    resolved = Path(raw_path).resolve()
    for allowed in self._allowed_dirs:
        if resolved.is_relative_to(allowed.resolve()):
            return resolved
    allowed_str = "\n".join(f"  - {d}" for d in self._allowed_dirs)
    raise ValueError(
        f"Path '{raw_path}' is outside allowed directories.\n"
        f"Allowed directories:\n{allowed_str}"
    )
```

Both sides use `resolve()` for cross-platform compatibility (backslashes, symlinks, case differences on Windows).

### Error Behavior

When path is outside allowed dirs, the tool returns:
- The error message
- The complete list of allowed directories (so the agent knows where it can write)

### Tool Descriptions

Tool descriptions remain simple and generic (e.g., "Read a file"). Directory constraints are communicated via system prompt, not tool descriptions.

### Location

```
framework/memory/tools/
  __init__.py
  scoped_read.py
  scoped_write.py
  scoped_edit.py
  scoped_list.py
```

Memory-subsystem-specific tools, not general-purpose tools.

### Instantiation by Role

**ArchiveSummarizer:**
```python
archive_dir = archive_base / str(archive_id)
tools = [
    ScopedReadFileTool(allowed_dirs=[archive_dir]),
    ScopedWriteFileTool(allowed_dirs=[archive_dir]),
    ScopedEditFileTool(allowed_dirs=[archive_dir]),
    ScopedListTool(allowed_dirs=[archive_dir]),
]
# session.jsonl written by framework, NOT accessible via tools
```

**KnowledgeConsolidator:**
```python
archive_dirs = [archive_base / str(aid) for aid in unprocessed_ids]
knowledge_dir = knowledge_base_path
tools = [
    ScopedReadFileTool(allowed_dirs=archive_dirs + [knowledge_dir]),
    ScopedWriteFileTool(allowed_dirs=[knowledge_dir]),
    ScopedEditFileTool(allowed_dirs=[knowledge_dir]),
    ScopedListTool(allowed_dirs=archive_dirs + [knowledge_dir]),
]
# session.jsonl written by framework, NOT accessible via tools
```

Note: Session storage (`{data_dir}/summarizer/{id}/`, `{data_dir}/consolidator/{cursor}/`) is framework-managed only — never added to scoped tool allowed_dirs.

## Section 3: Cleanup Four-Step Flow

### Current Flow → Target Flow

```
Current (cleanup_session):
  1. trigger check
  2. sanitize tool chains
  3. compute keep/prune boundary
  4. commit session (replace messages)
  5. archive (generate → write JSONL)
  6. pruned catalog (write index.jsonl)

Target (cleanup_session):
  1. trigger check
  2. sanitize tool chains
  3. compute keep/prune boundary
  4. Step 1: archive agent generates → archive/{id}/context.md, knowledge.md, index.md
  5. Step 2: PrunedManager full index refresh from all index.md files
  6. Step 3: commit session (replace messages + backup)
  7. Step 4: archive_id increment commit (state.json update)
```

### Key Changes

**Archive generation moved before session cleanup** to ensure:
- Archive failure doesn't lose data (session not yet cleaned)
- Pruned index is ready before session cleanup
- Archive_id only increments after all steps succeed

### Idempotency

- `next_archive_id` computed from `state.json` but NOT written until Step 4.
- Step 1 checks if `archive/{next_archive_id}/` exists and is complete (all three MD files present and non-empty):
  - Complete → skip agent call, reuse existing files.
  - Incomplete → overwrite and regenerate.
- If Steps 2–3 fail, archive_id is not incremented; next retry can reuse the same directory.

### Failure Handling

- **Step 1 fails** → fallback to old PrunedManager path (generate index.jsonl from pruned messages). Continue Steps 3–4 but do NOT increment archive_id.
- **Step 2 fails** → same fallback.
- **Step 3 fails** → archive generated but archive_id not incremented; next retry reuses archive directory.

### Pruned Index Refresh (Step 2)

Full rebuild, not incremental append:
1. Traverse all archive directories (1, 2, ..., latest).
2. Read each `index.md`.
3. Merge into complete pruned index (deduplicate, sort).
4. Overwrite PrunedManager's storage.

This ensures PrunedManager always shows the agent a **complete view** of all archives.

### cleanup_session Signature

```python
async def cleanup_session(
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    # ... existing parameters ...
    archive_agent: ArchiveSummarizer | None = None,  # NEW
) -> CleanupResult:
```

## Section 4: ArchiveSummarizer

### Role

Replaces `DualLLMArchiveGenerationStrategy`. An agent with tools that generates three MD files from pruned messages.

### Trigger

Called in `cleanup_session` Step 1.

### Input

- **pruned messages**: messages trimmed by this cleanup cycle
- **archive_id**: allocated archive_id (= invocation_id)
- **archive_dir**: full path to `archive/{id}/`

### System Prompt

```
## Task
Analyze the conversation transcript below and write three files.

## Allowed Directories
You can ONLY read and write files in these directories:
  - {archive_dir}

## Output Files

### context.md
Conversation summary for context injection.
- Include: Situation, Decisions, Completed Work, Open Threads, Evidence
- Max {context_max_chars} characters (default: 500)
- Output empty file if transcript has no useful context

### knowledge.md
Durable memory candidates extracted from transcript.
- Include: User Facts, Project Facts, Decisions, Reusable Lessons
- Max {knowledge_max_chars} characters (default: 600)
- Do NOT capture negative claims; capture the FIX instead
- Output empty file if no durable candidates

### index.md
Ultra-concise index entry for the pruned catalog.
- 1-3 lines, max {index_max_chars} characters (default: 100)
- Format: topic description + time range
- This is a search index — keep it MINIMAL

## Execution Rules
- This is a SINGLE-TURN task. You receive the transcript once, write the output files, and stop.
- No further user input will follow. Do your best analysis now.
- Write all three files using the provided tools, then stop.
- If a file would have no useful content, write an empty file.
- Work fast — minimize tool call rounds. Ideally write all files in one pass.
```

### Execution Flow

```
1. Framework creates archive/{id}/ directory
2. Framework builds AgentContext:
   - system_prompt: template above (filled with parameters)
   - history: [user message = formatted transcript]
   - tool_manager: 4 ScopedFileTools (allowed_dirs=[archive_dir])
   - max_iterations: 20
3. Agent runs ReAct loop
4. Agent writes context.md / knowledge.md / index.md via tools
5. Returns AgentResult
```

### Size Constraints (configurable)

| File | Default max chars | Rationale |
|------|-------------------|-----------|
| context.md | 500 | Injected truncated to 150 chars/entry, 3 entries ≈ 450 |
| knowledge.md | 600 | Consumed by DreamEngine, must be concise |
| index.md | 100 | Ultra-concise index, 1-3 lines |

### Relationship to DualLLMArchiveGenerationStrategy

- `DualLLMArchiveGenerationStrategy` retained for transition but not used by default.
- `cleanup_session` checks `archive_agent`: if present → new path; if absent → old strategy.

## Section 5: KnowledgeConsolidator

### Role

Replaces `DreamEngine.consolidate()` two-phase LLM calls. An agent with tools that reads archive knowledge.md files and updates knowledge files.

### Trigger

Same as current DreamEngine — offline/idle trigger, processes unprocessed archive entries.

### Tool Scope

```python
archive_dirs = [archive_base / str(aid) for aid in unprocessed_ids]
knowledge_dir = knowledge_base_path
tools = [
    ScopedReadFileTool(allowed_dirs=archive_dirs + [knowledge_dir]),
    ScopedWriteFileTool(allowed_dirs=[knowledge_dir]),
    ScopedEditFileTool(allowed_dirs=[knowledge_dir]),
    ScopedListTool(allowed_dirs=archive_dirs + [knowledge_dir]),
]
```

### System Prompt

```
## Task
Read the knowledge archive files listed below, analyze them, and update
the knowledge files (SOUL.md, USER.md, MEMORY.md) accordingly.

## Allowed Directories
You can ONLY access files in these directories:
  - {archive_dirs_formatted}
  - {knowledge_dir}

## Available Archive Files
{archive_file_list}

## Execution Rules
- This is a SINGLE-TURN task. Read archives, analyze, update knowledge, then stop.
- No further user input will follow. Do your best analysis now.
- Use edit for small changes, write for full replacement
- Preserve existing knowledge unless contradicted
- Remove stale/redundant content
- Max iterations: 20

## File Size Constraints
- Each knowledge file must stay under {max_file_chars} characters
- If a file grows too large, consolidate it

## Knowledge Files
- SOUL.md: agent identity, core principles — rarely changes
- USER.md: user profile, preferences
- MEMORY.md: long-term facts, project context

## What to capture (priority order)
1. USER CORRECTIONS — "stop doing X", "I prefer Y"
2. USER PREFERENCES — name, timezone, language, style
3. DESIGN DECISIONS — confirmed choices and rationale
4. SOLUTIONS — verified approaches (not derivable from code)
5. PERSONALITY / BEHAVIOR — new principles for the agent

## What to skip
- Code patterns derivable from source
- Git history, commit SHAs, PR/issue numbers
- Tool invocation details, raw tool outputs
- Temporary errors that were resolved
- Anything already in current file content
```

### Current vs Target

| Aspect | Current | Target |
|--------|---------|--------|
| LLM calls | Two-phase (fact_extraction + per-file update) | Agent decides autonomously |
| Input source | JSONL archive summary field | Direct read of knowledge.md files |
| Output method | `MemoryUpdate` objects → `apply_update()` | Agent directly edit/write knowledge files |
| Cursor management | `commit_cursor` + `prune_consumed_pairs` | Unchanged — framework commits after agent returns |

### Cursor Management (Framework Layer)

After agent completes, framework still:
1. `commit_cursor("dream", latest_archive_id)` — mark consumed
2. `prune_consumed_pairs()` — clean consumed archive directory pairs

Not in agent's tool scope — executed by `DreamEngine.run()` after agent returns.

### Size Constraints (configurable)

| File | Default max chars |
|------|-------------------|
| SOUL.md | 4096 |
| USER.md | 4096 |
| MEMORY.md | 8192 |

## Section 6: Injection Layer Changes

### Knowledge Injection — Unchanged

`_inject_knowledge()` remains identical:
- `retrieve_knowledge()` → `FullDumpKnowledgeStrategy` → `<agent_knowledge>` XML
- No truncation changes
- Only the update method changes (KnowledgeConsolidator instead of two-phase LLM)

### Archive Injection — Major Change

```
Current:
  get_history_entries() → read JSONL → parse summary field
  → <historical_context><record summary="..."/></historical_context>

Target:
  ArchiveStorage.read_archive_file(id, "context.md")
  → descending by archive_id, read latest N context.md files
  → truncate each to 150 chars
  → <historical_context><record archive_id="42" file="...">content</record></historical_context>
```

When truncated, the `<record>` element includes `file` attribute pointing to full context.md path so agent can read the complete file if needed.

### Pruned Injection — Data Source Change

```
Current:
  PrunedManager.get_injection_xml() → reads index.jsonl → XML

Target:
  PrunedManager full-refreshes from all archive index.md files
  → get_injection_xml() reads refreshed index
  → XML format unchanged (<memory_archives><directory path="..."/></memory_archives>)
```

### Injection Priority (unchanged)

| Priority | Content | Change |
|----------|---------|--------|
| 100 | Knowledge (SOUL + USER + MEMORY) | None |
| 90 | Pruned catalog | Data source: index.jsonl → index.md |
| 70 | Archive recent | Data source: JSONL → context.md |
| 60 | Provider blocks | None |
| 50 | Provider prefetch | None |

### Truncation Defaults (configurable)

| Content | Truncation rule |
|---------|----------------|
| Archive context.md | 150 chars per entry, max 3 entries |
| Pruned index.md | 80 chars per entry, show all (or by budget) |
| Knowledge | Unchanged — existing `FullDumpKnowledgeStrategy` |

## Section 7: Integration & Transition

### Component Relationship Diagram

```
                    cleanup_session()
                         │
          ┌──────────────┼──────────────────┐
          ▼              ▼                  ▼
    Step 1:         Step 2:            Step 3:         Step 4:
    ArchiveAgent    PrunedManager      Session         Archive ID
    (ReAct+tools)   full index refresh cleanup+backup  increment commit
          │              │                  │              │
          ▼              ▼                  ▼              ▼
    archive/{id}/    PrunedManager      session         state.json
      context.md     index storage      messages        updated
      knowledge.md      │
      index.md          ▼
                   injection shows
                   to main agent
                         │
                    DreamEngine idle trigger
                         │
                         ▼
                 KnowledgeConsolidator
                 (ReAct+tools)
                    reads knowledge.md → edits knowledge/
                    reads archive/{id}/
                         │
                         ▼
                    commit_cursor()
                    prune_consumed_pairs()
```

### Changed Files

| File/Module | Change Type | Description |
|-------------|-------------|-------------|
| `framework/memory/archive_generation.py` | Deprecate | `DualLLMArchiveGenerationStrategy` retained but not default |
| `framework/agents/summarizer/` | Rewrite | ArchiveSummarizer + KnowledgeConsolidator |
| `framework/memory/tools/` | New | Four ScopedFileTools |
| `framework/memory/layers/archive.py` | Rewrite storage | JSONL → MD directory model |
| `framework/memory/cleanup.py` | Refactor | Four-step flow + archive_agent parameter |
| `framework/memory/injection/full_injection.py` | Modify | Archive injection: JSONL → MD |
| `framework/memory/consolidation/dream_engine.py` | Rewrite | Two-phase LLM → KnowledgeConsolidator agent |
| `framework/memory/pruned/` | Modify | Full refresh index logic; data source supports index.md |
| `framework/memory/prompts/` | New/modify | New system prompt templates |
| `framework/ioc/factories/memory.py` | Modify | Build ArchiveSummarizer + KnowledgeConsolidator |

### Unchanged Parts

- `SessionMemoryManager` — session layer untouched
- `ScopedKnowledgeMemoryManager` — knowledge CRUD unchanged; only update trigger changes
- `FullDumpKnowledgeStrategy` — knowledge retrieval/truncation unchanged
- `KnowledgeMemoryManager` ABC — interface unchanged
- `MemorySystemContextManager` — unchanged
- `RestrictedInjectionPolicy` — subagent injection unchanged
- Existing tests for unchanged parts continue to pass

### Workspace Switching (cd/exit) Compatibility

Archive and knowledge storage paths are rooted at `workspace_context.data_dir` (`current / .modex`).

When workspace switches via `cd`/`exit`:
1. **Agent idle guarantee**: `active_checker` ensures no agents are running during switch — ArchiveSummarizer and KnowledgeConsolidator cannot be mid-execution.
2. **Memory system rebuild**: `_on_ws_stop_and_rebuild` rebuilds the entire `DefaultMemorySystem`, including new `ArchiveStorage`, `ArchiveSummarizer`, and `KnowledgeConsolidator` instances pointing at the new `data_dir`.
3. **ScopedFileTools allowed_dirs are dynamic**: Built at invocation time (`archive_base / str(archive_id)`), not at construction time — automatically correct after rebuild.
4. **No design changes needed**: The existing callback-based switch mechanism fully covers the new components.

### Transition Strategy

- `cleanup_session`: if `archive_agent` parameter provided → new four-step flow; otherwise → old strategy
- `DreamEngine`: if `KnowledgeConsolidator` provided → use agent; otherwise → old two-phase LLM
- Gradual migration: deploy ArchiveSummarizer first, verify stability, then switch DreamEngine
