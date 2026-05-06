# Search Tools Design — Coding-Agent-Style File Search for ModexAgent

**Date**: 2026-05-06  
**Status**: Draft (pending review)  
**Scope**: `framework/tools/standard/search_tool.py` + `examples/bot_project` integration  

---

## 1. Goal

Add two standard search tools (`search_files`, `find_files`) to the framework's standard tool set, enabling agents to perform codebase-wide text search and file discovery without relying on the `shell` tool. This improves:
- **Safety**: Search tools are read-only, no arbitrary command execution
- **Structured output**: Consistent JSON-like result format for LLM parsing
- **Cross-platform**: Works on Windows, Linux, macOS without requiring external binaries
- **Performance**: Uses fastest available backend per platform (`ripgrep` > `git grep` > Python `re`)

---

## 2. Tools

### 2.1 `search_files` — Text Search in File Contents

Search file contents for a text pattern or regular expression, with configurable result limits and context lines.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | — | Search pattern (regex or literal) |
| `path` | string | No | `"."` | Directory to search in |
| `file_pattern` | string | No | `"*"` | Glob filter for files (e.g. `"*.py"`) |
| `regex` | boolean | No | `true` | If true, `query` is a regex; if false, literal text |
| `max_results` | integer | No | `50` | Max matches to return (hard cap 200) |
| `context_lines` | integer | No | `2` | Lines of context before/after each match |

**Return format** (structured text):

```
Found 3 matches in 2 files:

framework/core/tool_manager.py:45
  43 | """工具管理器 - 抽象基类和实现
  44 |
> 45 | 提供 ToolManager 抽象层，支持工具注册、配置和并行执行调度。
  46 | """
  47 |

framework/core/tool_manager.py:104
  102 |     @abstractmethod
  103 |     async def execute(self, **kwargs) -> Any:
> 104 |         """执行工具
  105 |
  106 |         Args:

[... 1 more match ...]
```

### 2.2 `find_files` — File Discovery by Name Pattern

Find files matching a glob pattern within a directory tree.

**Parameters**:

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pattern` | string | Yes | — | Glob pattern (e.g. `"*.py"`, `"**/*test*.py"`) |
| `path` | string | No | `"."` | Directory to search in |
| `max_results` | integer | No | `100` | Max files to return (hard cap 500) |

**Return format**:

```
Found 42 files matching "*.py":

framework/core/tool_manager.py
framework/core/agent.py
framework/tools/standard/file_tool.py
...

[42 total, showing first 100]
```

---

## 3. Architecture

### 3.1 Backend Selection Strategy (per-tool-call)

```
search_files:
  ├─ shutil.which("rg") exists? ──→ Use ripgrep (subprocess, --json output)
  ├─ .git directory exists AND shutil.which("git") exists? ──→ Use git grep
  └─ Fallback ──→ Python re + pathlib (pure Python, always works)

find_files:
  ├─ shutil.which("fd") exists? ──→ Use fd (subprocess)
  └─ Fallback ──→ Python pathlib.Path.rglob (pure Python)
```

### 3.2 Why This Strategy

| Backend | When Used | Pros | Cons |
|---------|-----------|------|------|
| **ripgrep** | Available on any platform | Fastest, respects `.gitignore`, Unicode-aware, parallel by default | Requires binary installation |
| **git grep** | Git repo + no rg | Fast, respects `.gitignore`, no extra install if git exists | Only works inside git repos |
| **Python re** | Universal fallback | Zero dependencies, 100% portable, full regex support | Slower on very large repos |
| **fd** | Available on any platform | Fast, respects `.gitignore`, user-friendly glob | Requires binary installation |
| **pathlib** | Universal fallback | Zero dependencies, 100% portable | Slower on very large repos |

### 3.3 Cross-Platform Considerations

- **Path handling**: All paths normalized via `pathlib.Path` (handles `/` vs `\` automatically)
- **Line endings**: Read files in text mode with `newline=""` (preserves `\r\n` vs `\n`)
- **Binary files**: Skip binary files (detected by null byte check or rg's built-in filtering)
- **Encoding**: Default UTF-8 with fallback to UTF-8 + `errors="replace"`
- **Subprocess**: Use `subprocess.run` with `shell=False` (safer, works cross-platform)

---

## 4. Implementation Details

### 4.1 File: `framework/tools/standard/search_tool.py`

Two tool classes:

```python
class SearchFilesTool(Tool):
    """Search file contents for a pattern."""
    
    # Backend detection (cached per instance)
    _has_rg: bool | None = None
    _has_git: bool | None = None
    
    async def execute(self, query, path=".", file_pattern="*", 
                      regex=True, max_results=50, context_lines=2):
        # 1. Detect and select backend
        # 2. Execute search
        # 3. Format and truncate results
        pass

class FindFilesTool(Tool):
    """Find files by name pattern."""
    
    _has_fd: bool | None = None
    
    async def execute(self, pattern, path=".", max_results=100):
        # 1. Detect and select backend
        # 2. Execute search
        # 3. Format and truncate results
        pass
```

### 4.2 Result Truncation (Pagination)

Both tools enforce pagination to prevent token overflow:

1. **Hard caps**: `search_files` max 200 matches, `find_files` max 500 files
2. **Result format always includes**: total count, file path, line numbers, matched content with context
3. **Truncation indicator**: When results exceed `max_results`, append `[... N more matches not shown]`

### 4.3 Error Handling

| Scenario | Behavior |
|----------|----------|
| Path does not exist | Return `"Error: Directory not found: {path}"` |
| Path is not a directory | Return `"Error: Not a directory: {path}"` |
| Invalid regex | Return `"Error: Invalid regex pattern: {details}"` |
| No matches found | Return `"No matches found for '{query}' in {path}"` |
| Backend subprocess fails | Log warning, auto-fallback to Python implementation |
| Binary file encountered | Skip (rg does this automatically; Python fallback checks for null bytes) |

---

## 5. Integration into bot_project

### 5.1 Config (`bot_config.yml`)

Add `search_tools` section under `tools`:

```yaml
tools:
  file_tools: {}
  shell_tools: {}
  search_tools:      # NEW
    enabled: true
```

### 5.2 Registration (`builders.py`)

In `_register_tools()` (main agent) and `_build_peer_tool_manager()` (peers/subagents):

```python
search_tools_config = tools_config.get("search_tools", {})
if search_tools_config.get("enabled", True):
    from framework.tools.standard import SearchFilesTool, FindFilesTool
    self.tool_manager.register(SearchFilesTool())
    self.tool_manager.register(FindFilesTool())
    print("   [OK] Search tools registered (search_files, find_files)")
```

### 5.3 Default Behavior

- **Main agent**: Search tools enabled by default (like file_tools)
- **Peers/Subagents**: Inherit from their `tools` config; if `search_tools` omitted, defaults to enabled if `tools` section exists
- **No permission configuration needed**: Search tools are read-only and safe

---

## 6. Testing Plan

### 6.1 Unit Tests (`tests/unit/tools/test_search_tools.py`)

1. **Backend detection**: Test `_has_rg`, `_has_git`, `_has_fd` detection logic
2. **Python fallback search**: Create temp files, search with regex and literal, verify results
3. **Pagination**: Create files with many matches, verify truncation at max_results
4. **Cross-platform path handling**: Test with nested directories, spaces in names
5. **Error cases**: Nonexistent path, invalid regex, binary file skip

### 6.2 Integration Tests

1. **Bot project startup**: Verify search tools appear in tool registry
2. **Agent tool call**: Mock LLM calling `search_files`, verify structured output

---

## 7. Open Questions

1. **Should `search_files` support `--include` / `--exclude` directory filtering?**
   - Pro: More powerful for large monorepos
   - Con: More parameters = more complex prompt
   - **Recommendation**: Start without; add if requested

2. **Should results be returned as JSON instead of formatted text?**
   - Pro: Easier for LLM to parse programmatically
   - Con: Current framework tools return text; JSON might be unexpected
   - **Recommendation**: Formatted text (matches existing tools like `list_dir`)

---

## 8. Alternatives Considered

| Alternative | Rejected Because |
|-------------|-----------------|
| Only ripgrep (no fallback) | Breaks on Windows without rg installed |
| Only Python re (no rg) | Slower on large repos; wastes user's rg installation if present |
| Single `search` tool (merged find+search) | LLM confusion: two distinct use cases ("find by name" vs "search in contents") |
| Return JSON | Inconsistent with existing standard tools; formatted text is LLM-friendly |

---

## 9. Success Criteria

- [ ] `search_files` works on Windows, Linux, macOS without external dependencies
- [ ] Uses ripgrep when available for performance
- [ ] Results are paginated and never exceed reasonable token limits
- [ ] Integrated into bot_project main agent + peer agents + subagents
- [ ] All unit tests pass
- [ ] Bot project starts successfully with search tools registered
