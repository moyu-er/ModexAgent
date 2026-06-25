<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# standard

## Purpose
Built-in agent-facing tools for file system operations and content search. Provides read/write/edit/ls/find file tools and ripgrep/git-grep/Python-re based content search tools. These are the core tools available to all agents by default.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `file_tool.py` | `FileReadTool`, `FileWriteTool`, `FileEditTool`, `FileListTool` — concise file operations. Handles path resolution, curly-quote normalization, and atomic file writes |
| `search_tool.py` | `SearchFilesTool`, `FindFilesTool` — content search and file discovery. Auto-detects fastest available backend: ripgrep (rg) > git grep > Python re for search; fd > Python pathlib.rglob for find |

## For AI Agents

### Working In This Directory
- File tools operate on resolved absolute paths — no workspace scoping (that's handled by `workspace_scoped.py` wrappers)
- `FileEditTool` supports exact string replacement with `replace_all` mode for bulk renaming
- Search tools auto-detect the best available backend per platform — no manual configuration needed
- Common exclusions: `.git`, `.venv`, `node_modules`, `__pycache__`, etc. are excluded by default from all searches
- `SearchFilesTool` has configurable `max_results` (default: 50, hard cap: 200) and `context_lines` for surrounding context

### Common Patterns
- `FileReadTool.execute({"path": "path/to/file"})` — reads file content
- `FileWriteTool.execute({"path": "path/to/file", "content": "..."})` — writes/overwrites file
- `FileEditTool.execute({"path": "...", "old_string": "...", "new_string": "..."})` — replaces first occurrence
- `SearchFilesTool.execute({"query": "pattern", "path": "."})` — search with regex
- `FindFilesTool.execute({"pattern": "*.py", "path": "."})` — find files by glob

## Dependencies

### Internal
- `modex_agent.core.tool_manager` — `Tool` ABC

### External
- `ripgrep` (optional) — fastest search backend on supported platforms
- `fd` (optional) — fastest file discovery backend
- `git` — fallback search via `git grep`

<!-- MANUAL -->
