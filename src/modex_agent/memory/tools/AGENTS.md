<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-10 | Updated: 2026-06-10 -->

# memory/tools

## Purpose

Agent-facing scoped file tools (read/write/edit/list) for summarizer agents. The 6 experience tools moved to the `experience` capability package (plan §10).

## Key Files

| File | Description |
|------|-------------|
| `scoped_read.py` | `ScopedReadFileTool` — read-only file tool restricted to `allowed_dirs` |
| `scoped_write.py` | `ScopedWriteFileTool` — write tool restricted to `allowed_dirs` |
| `scoped_edit.py` | `ScopedEditFileTool` — find-and-replace edit tool restricted to `allowed_dirs` |
| `scoped_list.py` | `ScopedListDirTool` — directory listing restricted to `allowed_dirs` |
| `_utils.py` | Shared utilities for scoped file operations |
| `__init__.py` | Public API re-exports |

## Experience Tool Architecture

Each experience tool wraps a standard file tool and adds:
1. **Name validation** — `_validate_name()` checks format (alphanumeric, hyphens, dots, underscores)
2. **Path resolution** — `ExperiencePathResolver` enforces containment within experience root
3. **Metadata tracking** — bumps use_count/view_count via `ExperienceMetaStore`
4. **Auto-correction** — `auto_correct_frontmatter_name()` after write/edit
5. **Validation** — `validate_experience_md()` after write/edit, returns XML with errors/warnings

```
ExperienceWriteTool.execute(name, content)
  → validate_name(name)
  → resolver.resolve(name) → contained Path
  → WriteFileTool.execute(path, content)
  → auto_correct_frontmatter_name(exp_dir)
  → validate_experience_md(content)
  → return XML result with validation status
```

## For AI Agents

### Working In This Directory
- Experience tools delegate to standard file tools — no direct filesystem access
- `ExperiencePathResolver` accepts `Path | Callable[[], Path]` for workspace-safe resolution
- All tools return XML-formatted results for LLM consumption
- Scoped tools enforce `allowed_dirs` — summarizer agents cannot escape their target directory

### Dependencies
- `modex_agent.core.tool_manager` — `Tool` base class
- `modex_agent.utils.xml` — `xml_text()`
