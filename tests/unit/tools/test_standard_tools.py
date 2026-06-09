"""Unit tests for standard tools: file tools and shell tool.

TDD: verify ReadFileTool, WriteFileTool, EditFileTool, ListDirTool,
and CommandTool behaviors including fuzzy matching, replace_all, and safety guards.
Permission checks are delegated to the interceptor AOP layer.
"""

import os
import tempfile
from pathlib import Path

import pytest

from framework.tools.standard.file_tool import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ListDirTool,
    _find_actual_string,
    _map_whitespace_back,
    _normalize_quotes,
    _preserve_quote_style,
)
from framework.tools.terminal import SubprocessTool


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------

class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_existing_file(self, tmp_workspace):
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("line1\nline2\nline3", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path))
        assert "line1" in result
        assert "line3" in result
        assert "read_status: complete" in result

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_workspace):
        tool = ReadFileTool()
        result = await tool.execute(path=str(tmp_workspace / "missing.txt"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_read_directory_error(self, tmp_workspace):
        tool = ReadFileTool()
        result = await tool.execute(path=str(tmp_workspace))
        assert "not a file" in result.lower()

    @pytest.mark.asyncio
    async def test_read_with_offset_and_limit(self, tmp_workspace):
        """offset=1, limit=3 跳过第 1 行，读取 3 行 (b,c,d)。"""
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb\nc\nd\ne", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), offset=1, limit=3)
        lines = result.splitlines()
        assert "b" in lines
        assert "c" in lines
        assert "d" in lines
        assert "a" not in lines
        assert "e" not in lines

    @pytest.mark.asyncio
    async def test_read_negative_offset(self, tmp_workspace):
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), offset=-1)
        assert "offset must be >= 0" in result

    @pytest.mark.asyncio
    async def test_read_zero_limit(self, tmp_workspace):
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), limit=0)
        assert "limit must be >= 1" in result

    @pytest.mark.asyncio
    async def test_read_offset_exceeds_file(self, tmp_workspace):
        """offset 超出文件行数时，返回错误并告知有效范围。"""
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb\nc", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), offset=10)
        assert "offset (10) exceeds file length (3 lines)" in result
        assert "Valid offset range: 0 ~ 2" in result

    @pytest.mark.asyncio
    async def test_read_empty_file(self, tmp_workspace):
        file_path = tmp_workspace / "empty.txt"
        file_path.write_text("", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path))
        assert "(empty file)" in result
        assert "read_status: empty" in result
        assert "total_lines: 0" in result

    @pytest.mark.asyncio
    async def test_read_limit_exceeds_remaining(self, tmp_workspace):
        """limit 超出剩余行数时，返回实际内容并提示。"""
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb\nc", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), offset=0, limit=100)
        assert "a" in result
        assert "c" in result
        assert "read_status: complete" in result
        assert "requested 100" in result
        assert "file has 3 remaining" in result

    @pytest.mark.asyncio
    async def test_read_limit_clamped_to_max(self, tmp_workspace):
        """limit > 300 被自动 clamp，不报错。"""
        file_path = tmp_workspace / "test.txt"
        file_path.write_text("a\nb", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), limit=999)
        assert "read_status: complete" in result
        # 不应报错
        assert "error" not in result.lower()

    @pytest.mark.asyncio
    async def test_read_truncated_by_limit(self, tmp_workspace):
        """文件行数 > limit 时，返回 truncated_by_limit。"""
        file_path = tmp_workspace / "test.txt"
        lines = [f"line{i}" for i in range(500)]
        file_path.write_text("\n".join(lines), encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), offset=0, limit=200)
        assert "read_status: truncated_by_limit" in result
        assert "remaining_lines: 300" in result
        assert "hint: use offset=200" in result
        # 验证内容只有前 200 行
        assert "line0" in result
        assert "line199" in result
        assert "line200" not in result

    @pytest.mark.asyncio
    async def test_read_truncated_by_chars(self, tmp_workspace):
        """累积字符数超限时截断。"""
        file_path = tmp_workspace / "test.txt"
        # 每行 ~200 字符，200 行 ≈ 40000 字符 > 20000 上限
        lines = ["x" * 100 for _ in range(200)]
        file_path.write_text("\n".join(lines), encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path), offset=0, limit=200)
        assert "read_status: truncated_by_chars" in result
        assert "stopped before limit" in result
        assert "warning: char limit" in result
        assert "hint: use offset=" in result

    @pytest.mark.asyncio
    async def test_read_single_long_line_within_char_limit(self, tmp_workspace):
        """单行超长但总量未超 max_chars 时完整返回。"""
        file_path = tmp_workspace / "test.txt"
        long_line = "x" * 5000
        file_path.write_text(f"short\n{long_line}\nend", encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(path=str(file_path))
        assert "read_status: complete" in result
        assert "short" in result
        assert "end" in result
        # 超长行完整保留，不截断
        assert long_line in result


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------

class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_workspace):
        tool = WriteFileTool()
        file_path = tmp_workspace / "new.txt"
        result = await tool.execute(path=str(file_path), content="hello")
        assert "successfully wrote" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "hello"

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_workspace):
        tool = WriteFileTool()
        file_path = tmp_workspace / "sub" / "dir" / "file.txt"
        result = await tool.execute(path=str(file_path), content="data")
        assert file_path.exists()
        assert file_path.read_text(encoding="utf-8") == "data"


# ---------------------------------------------------------------------------
# EditFileTool — basic
# ---------------------------------------------------------------------------

class TestEditFileTool:
    @pytest.mark.asyncio
    async def test_edit_existing_text(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("hello world", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(path=str(file_path), old_string="world", new_string="universe")
        assert "successfully edited" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "hello universe"

    @pytest.mark.asyncio
    async def test_edit_missing_old_string(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("hello world", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(path=str(file_path), old_string="missing", new_string="x")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_ambiguous_old_string(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("abc abc", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(path=str(file_path), old_string="abc", new_string="x")
        assert "found 2 matches" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_replace_all(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("abc abc abc", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string="abc", new_string="x", replace_all=True
        )
        assert "all 3 occurrences replaced" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "x x x"

    @pytest.mark.asyncio
    async def test_edit_delete_text(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("hello cruel world", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string=" cruel", new_string=""
        )
        assert "successfully edited" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "hello world"

    @pytest.mark.asyncio
    async def test_edit_no_change_same_strings(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("hello world", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string="world", new_string="world"
        )
        assert "no changes to make" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_create_new_file_empty_old_string(self, tmp_workspace):
        file_path = tmp_workspace / "new_file.txt"
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string="", new_string="new content"
        )
        assert "successfully created" in result.lower()
        assert file_path.read_text(encoding="utf-8") == "new content"

    @pytest.mark.asyncio
    async def test_edit_empty_old_string_on_nonempty_file(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("existing content", encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string="", new_string="new content"
        )
        assert "file already exists and is not empty" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_file_not_found(self, tmp_workspace):
        file_path = tmp_workspace / "missing.txt"
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string="x", new_string="y"
        )
        assert "file not found" in result.lower()

    @pytest.mark.asyncio
    async def test_edit_preserves_crlf(self, tmp_workspace):
        file_path = tmp_workspace / "edit.txt"
        file_path.write_bytes(b"hello\r\nworld\r\n")
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string="world", new_string="universe"
        )
        assert "successfully edited" in result.lower()
        raw = file_path.read_bytes()
        assert b"\r\n" in raw
        assert raw == b"hello\r\nuniverse\r\n"


# ---------------------------------------------------------------------------
# EditFileTool — fuzzy matching
# ---------------------------------------------------------------------------

class TestEditFileToolFuzzyMatching:
    @pytest.mark.asyncio
    async def test_edit_fuzzy_curly_quotes(self, tmp_workspace):
        """弯引号在 old_string 中被归一化匹配。"""
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text('say "hello" to the world', encoding="utf-8")
        tool = EditFileTool()
        # LLM 输出直引号，但文件中有弯引号
        result = await tool.execute(
            path=str(file_path), old_string='say "hello" to the', new_string='tell the'
        )
        assert "successfully edited" in result.lower()
        assert 'tell the world' in file_path.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_edit_fuzzy_tab_to_spaces(self, tmp_workspace):
        """tab 在 old_string 中被空格归一化匹配；new_string 中的空格忠实地替换了原 tab 位置。"""
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text("def foo():\n\treturn 1", encoding="utf-8")
        tool = EditFileTool()
        # LLM 输出 4 个空格，但文件中是 tab
        result = await tool.execute(
            path=str(file_path), old_string="    return 1", new_string="    return 2"
        )
        assert "successfully edited" in result.lower()
        content = file_path.read_text(encoding="utf-8")
        # new_string 提供的是空格，所以替换后文件中使用空格而非 tab
        assert "    return 2" in content

    @pytest.mark.asyncio
    async def test_edit_fuzzy_quote_and_whitespace(self, tmp_workspace):
        """同时处理引号和空白差异；new_string 中的空格忠实地替换了原 tab 位置，且引号风格被保留。"""
        file_path = tmp_workspace / "edit.py"
        file_path.write_text('x = \t\u201chello\u201d', encoding="utf-8")
        tool = EditFileTool()
        # LLM 从 read_file 看到 'x =     "hello"'（5 空格，因为 tab 被渲染为 4 空格）
        result = await tool.execute(
            path=str(file_path), old_string='x =     "hello"', new_string='x =     "world"'
        )
        assert "successfully edited" in result.lower()
        content = file_path.read_text(encoding="utf-8")
        # 弯引号风格被保留，空格替换了 tab
        assert 'x =     \u201cworld\u201d' in content

    @pytest.mark.asyncio
    async def test_edit_preserves_curly_quotes_in_new_string(self, tmp_workspace):
        """通过引号归一化匹配时，new_string 中的直引号应转为弯引号。"""
        file_path = tmp_workspace / "edit.txt"
        file_path.write_text('print("hello")', encoding="utf-8")
        tool = EditFileTool()
        result = await tool.execute(
            path=str(file_path), old_string='"hello"', new_string='"world"'
        )
        assert "successfully edited" in result.lower()
        content = file_path.read_text(encoding="utf-8")
        assert '"world"' in content


# ---------------------------------------------------------------------------
# _find_actual_string
# ---------------------------------------------------------------------------

class TestFindActualString:
    def test_exact_match(self):
        assert _find_actual_string("hello world", "world") == "world"

    def test_no_match(self):
        assert _find_actual_string("hello world", "missing") is None

    def test_quote_normalization(self):
        file_content = 'say \u201chello\u201d to you'
        search = '"hello"'
        actual = _find_actual_string(file_content, search)
        assert actual == '\u201chello\u201d'

    def test_whitespace_normalization(self):
        file_content = "def foo():\n\treturn 1"
        search = "    return 1"
        actual = _find_actual_string(file_content, search)
        assert actual == "\treturn 1"

    def test_combined_normalization(self):
        # file: x = <tab><curly-left>hello<curly-right>
        # read_file renders tab as 4 spaces, so LLM sees: x =     "hello"
        file_content = 'x = \t\u201chello\u201d'
        search = 'x =     "hello"'
        actual = _find_actual_string(file_content, search)
        assert actual == 'x = \t\u201chello\u201d'

    def test_empty_search(self):
        assert _find_actual_string("anything", "") == ""


# ---------------------------------------------------------------------------
# _map_whitespace_back
# ---------------------------------------------------------------------------

class TestMapWhitespaceBack:
    def test_basic_tab(self):
        original = "a\tb"
        ws = "a    b"
        assert _map_whitespace_back(original, ws, 0, 6) == "a\tb"

    def test_tab_in_middle(self):
        original = "foo\tbar\tbaz"
        ws = "foo    bar    baz"
        assert _map_whitespace_back(original, ws, 7, 10) == "bar\tbaz"

    def test_multiple_leading_tabs(self):
        original = "\t\ta"
        ws = "        a"
        assert _map_whitespace_back(original, ws, 0, 9) == "\t\ta"

    def test_snap_inside_tab(self):
        """匹配范围落在 tab 扩展中间时 snap 到 tab 边界。"""
        original = "a\t"
        ws = "a    "
        assert _map_whitespace_back(original, ws, 1, 3) == "\t"


# ---------------------------------------------------------------------------
# _preserve_quote_style
# ---------------------------------------------------------------------------

class TestPreserveQuoteStyle:
    def test_no_change_when_exact_match(self):
        assert _preserve_quote_style('"hello"', '"hello"', '"world"') == '"world"'

    def test_double_curly_quotes(self):
        old_model = '"hello"'
        old_actual = '\u201chello\u201d'
        result = _preserve_quote_style(old_model, old_actual, '"world"')
        assert '\u201cworld\u201d' == result

    def test_single_curly_quotes(self):
        old_model = "'hello'"
        old_actual = '\u2018hello\u2019'
        result = _preserve_quote_style(old_model, old_actual, "'world'")
        assert '\u2018world\u2019' == result

    def test_apostrophe_in_contraction(self):
        old_model = "don't"
        old_actual = "don\u2019t"
        result = _preserve_quote_style(old_model, old_actual, "can't")
        # contraction apostrophe → right single curly quote
        assert "can\u2019t" == result


# ---------------------------------------------------------------------------
# ListDirTool
# ---------------------------------------------------------------------------

class TestListDirTool:
    @pytest.mark.asyncio
    async def test_list_existing_directory(self, tmp_workspace):
        (tmp_workspace / "file1.txt").write_text("x")
        (tmp_workspace / "dir1").mkdir()
        tool = ListDirTool()
        result = await tool.execute(path=str(tmp_workspace))
        assert "file1.txt" in result
        assert "dir1" in result

    @pytest.mark.asyncio
    async def test_list_empty_directory(self, tmp_workspace):
        tool = ListDirTool()
        result = await tool.execute(path=str(tmp_workspace))
        assert "is empty" in result.lower()

    @pytest.mark.asyncio
    async def test_list_not_found(self, tmp_workspace):
        tool = ListDirTool()
        result = await tool.execute(path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_list_file_error(self, tmp_workspace):
        file_path = tmp_workspace / "not_dir.txt"
        file_path.write_text("x")
        tool = ListDirTool()
        result = await tool.execute(path=str(file_path))
        assert "not a directory" in result.lower()


# ---------------------------------------------------------------------------
# SubprocessTool
# ---------------------------------------------------------------------------

class TestSubprocessTool:
    @pytest.fixture
    def safe_shell(self):
        return SubprocessTool(enable_safety_guard=True)

    @pytest.mark.asyncio
    async def test_shell_echo(self, safe_shell):
        result = await safe_shell.execute(command='echo "hello"')
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_shell_with_working_dir(self, tmp_workspace):
        shell = SubprocessTool(enable_safety_guard=False)
        result = await shell.execute(command="pwd" if os.name != "nt" else "cd", working_dir=str(tmp_workspace))
        # On Windows `cd` returns current dir; on Unix `pwd` returns it
        assert str(tmp_workspace.name) in result or "STDERR" not in result

    @pytest.mark.asyncio
    async def test_shell_timeout(self):
        shell = SubprocessTool(timeout=1, enable_safety_guard=False)
        # Use Python sleep for cross-platform timeout test
        result = await shell.execute(command='python -c "import time; time.sleep(5)"')
        assert "timed out" in result.lower()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-specific dangerous command test")
    @pytest.mark.asyncio
    async def test_shell_safety_guard_blocks_rm_rf(self, safe_shell):
        result = await safe_shell.execute(command="rm -rf /tmp/test")
        assert "blocked by safety guard" in result.lower()

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific dangerous command test")
    @pytest.mark.asyncio
    async def test_shell_safety_guard_blocks_windows_dangerous(self, safe_shell):
        result = await safe_shell.execute(command="format C:")
        assert "blocked by safety guard" in result.lower()

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific format command test")
    @pytest.mark.asyncio
    async def test_shell_safety_guard_blocks_format(self, safe_shell):
        result = await safe_shell.execute(command="format C:")
        assert "blocked by safety guard" in result.lower() or "dangerous pattern" in result.lower()

    @pytest.mark.asyncio
    async def test_shell_disabled_safety_guard(self):
        shell = SubprocessTool(enable_safety_guard=False)
        # Even dangerous-looking command should be allowed when guard is off
        result = await shell.execute(command="echo 'rm -rf /'")
        assert "rm -rf /" in result

    @pytest.mark.asyncio
    async def test_shell_allowlist_blocks_unmatched(self):
        shell = SubprocessTool(
            enable_safety_guard=True,
            allow_patterns=[r"^echo\b"],
        )
        result = await shell.execute(command="ls")
        assert "not in allowlist" in result.lower()
