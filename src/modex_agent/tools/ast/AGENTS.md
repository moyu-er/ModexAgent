<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# ast

## Purpose
AST-based code analysis and manipulation tools. Provides tree-sitter powered search (S-expression pattern matching) and replace operations on source code. Gracefully degrades when tree-sitter is not installed.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `engine.py` | Core tree-sitter pattern matching engine — `search_in_file()`, `search_in_directory()`, `replace_in_file()`. Uses tree-sitter S-expression queries. Graceful degradation when tree-sitter or language grammars are unavailable |
| `ast_search.py` | `AstGrepSearchTool(Tool)` — agent-facing tool for AST-aware code search. Parameters: pattern (S-expression), path, file pattern, language |
| `ast_replace.py` | `AstGrepReplaceTool(Tool)` — agent-facing tool for AST-aware code replacement. Dry-run by default. Parameters: pattern, replacement, path, file pattern, language, dry_run |

## For AI Agents

### Working In This Directory
- Tools use tree-sitter S-expression query syntax for pattern matching
- Example: `(function_definition name: (identifier) @name) @func` finds all function definitions
- Graceful degradation: if tree-sitter or required language grammars are missing, tools return a clear error message
- `ast_replace` defaults to dry-run mode — set `dry_run=false` to perform actual replacements
- Supports multiple languages via `_EXT_MAP` (Python, Java, etc. depending on installed grammars)

### Common Patterns
- Search: `AstGrepSearchTool.execute({"pattern": "(class_definition name: (identifier) @name) @cls", "path": "."})`
- Replace: `AstGrepReplaceTool.execute({"pattern": "(old_pattern)", "replacement": "(new_pattern)", "dry_run": true})`
- Engine functions return `SearchMatch` / `ReplaceMatch` dataclasses with file path, line numbers, and matched text

## Dependencies

### Internal
- `modex_agent.core.tool_manager` — `Tool` ABC
- `modex_agent.workspace.runtime` — `resolve_workspace_root`

### External
- `tree_sitter` (optional) — S-expression query engine
- `tree_sitter_python` (optional) — Python grammar
- `tree_sitter_java` (optional) — Java grammar

<!-- MANUAL -->
