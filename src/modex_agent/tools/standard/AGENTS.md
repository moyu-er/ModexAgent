<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-07-24 -->

# standard

## Purpose
Built-in agent-facing tools for file system operations, content search, and file discovery. Provides read/write/edit/ls tools, ripgrep/git-grep/Python-re based content search, and a ripgrep/fd/Python glob tool. These are the core tools available to all agents by default.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `file_tool.py` | `ReadFileTool`, `WriteFileTool`, `EditFileTool`, `ListDirTool` — concise file operations. Handles path resolution, curly-quote normalization, and atomic file writes |
| `search_tool.py` | `SearchFilesTool` (name=`grep`) — content search. Auto-detects fastest backend: ripgrep (rg) > git grep > Python re |
| `glob_tool.py` | `GlobTool` (name=`glob`) — file pattern matching. Backend chain: ripgrep (`rg --files --hidden --glob=<pattern> --glob=!**/.git/**`) > fd > Python `pathlib.rglob`. Supports brace expansion (all backends), gitignore (rg/fd native; Python fallback parses root `.gitignore`), dotfiles. Limit 100 (max 200) |
| `_ripgrep.py` | `RipgrepBackend` — shared `rg --files` subprocess invocation. Pydantic `RipgrepResult` (frozen). PATH-only resolution (no binary bundling). Used by `GlobTool`; `SearchFilesTool` has its own rg invocation (not yet migrated) |
| `_fallback.py` | Shared helpers for Python fallback backends — `expand_braces`, `load_gitignore`, `is_ignored`, `DEFAULT_EXCLUDES`. Used by both `GlobTool` and `SearchFilesTool` Python fallbacks |

## For AI Agents

### Working In This Directory
- File tools operate on resolved absolute paths — no workspace scoping (that's handled by `workspace_scoped.py` wrappers)
- `FileEditTool` supports exact string replacement with `replace_all` mode for bulk renaming
- `GlobTool` backend chain auto-detects: rg → fd → Python pathlib. rg and fd respect `.gitignore` natively; Python fallback parses root `.gitignore` and manually excludes common heavy directories
- Brace expansion (`*.{ts,tsx}`) works with all backends (rg/fd native; Python fallback expands manually)
- `SearchFilesTool` has configurable `max_results` (default: 50, hard cap: 200) and `context_lines` for surrounding context
- `_ripgrep.py` positive glob comes before exclusions (ripgrep's "last matching glob wins" rule)

### Common Patterns
- `ReadFileTool.execute({"path": "path/to/file"})` — reads file content
- `WriteFileTool.execute({"path": "path/to/file", "content": "..."})` — writes/overwrites file
- `FileEditTool.execute({"path": "...", "old_string": "...", "new_string": "..."})` — replaces first occurrence
- `SearchFilesTool.execute({"pattern": "regex", "path": "."})` — search file contents
- `GlobTool.execute({"pattern": "**/*.py", "path": "."})` — find files by glob

## Dependencies

### Internal
- `modex_agent.core.tool_manager` — `Tool` ABC

### External
- `ripgrep` (optional) — fastest search and glob backend on supported platforms
- `fd` (optional) — secondary glob backend
- `git` — fallback search via `git grep`

<!-- MANUAL -->
