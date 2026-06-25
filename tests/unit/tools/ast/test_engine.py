"""Tests for AST engine — covers graceful degradation and full search/replace.

Requires: pip install tree-sitter tree-sitter-python
"""

from __future__ import annotations

import pytest

from modex_agent.tools.ast.engine import (
    AST_UNAVAILABLE_MSG,
    AstMatch,
    AstNotAvailableError,
    AstParseError,
    AstQueryError,
    _EXT_MAP,
    _get_parser,
    _resolve_language,
    is_ast_available,
    replace_in_file,
    search_in_directory,
    search_in_file,
)

# ── Helpers ──

_AST_AVAILABLE = is_ast_available()
requires_ast = pytest.mark.skipif(not _AST_AVAILABLE, reason="tree-sitter not installed")


PY_SOURCE = """\
def hello():
    print("Hello, World!")

class Greeter:
    def greet(self, name):
        return f"Hi, {name}"

def goodbye():
    print("Goodbye!")
"""


# ── Graceful degradation tests (always run) ──

class TestGracefulDegradation:
    """When tree-sitter is not installed, behavior is well-defined."""

    def test_is_ast_available_returns_bool(self) -> None:
        """is_ast_available() always returns a bool."""
        result = is_ast_available()
        assert isinstance(result, bool)

    def test_get_parser_invalid_language_raises(self) -> None:
        """Invalid language raises AstNotAvailableError."""
        if not is_ast_available():
            with pytest.raises(AstNotAvailableError):
                _get_parser("nonexistent_language")
        else:
            with pytest.raises(AstNotAvailableError):
                _get_parser("nonexistent_language")


class TestExtMap:
    """File extension mapping for language lookup."""

    def test_python_ext_is_py(self) -> None:
        assert _EXT_MAP["python"] == (".py",)

    def test_java_ext_is_java(self) -> None:
        assert _EXT_MAP["java"] == (".java",)


# ── Real engine tests (skip if tree-sitter not installed) ──

@requires_ast
class TestResolveLanguage:
    """Language resolution with tree-sitter installed."""

    def test_python_language_resolves(self) -> None:
        lang = _resolve_language("python")
        assert lang is not None

    def test_java_unavailable_without_grammar(self) -> None:
        """Java should raise AstNotAvailableError unless tree-sitter-java is installed."""
        try:
            import tree_sitter_java  # noqa: F401
            _JAVA_AVAILABLE = True
        except ImportError:
            _JAVA_AVAILABLE = False

        if not _JAVA_AVAILABLE:
            with pytest.raises(AstNotAvailableError) as exc:
                _resolve_language("java")
            assert "java" in str(exc.value).lower()


@requires_ast
class TestSearchInFile:
    """Search for patterns in source strings."""

    def test_find_function_definitions(self) -> None:
        """Find all function definitions and capture their names."""
        matches = search_in_file(
            PY_SOURCE,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        func_names = [m.captures.get("name", "") for m in matches]
        assert "hello" in func_names
        assert "goodbye" in func_names
        # greet is a method, not a top-level function_definition
        assert "greet" in func_names  # method is inside class but still a function_definition

    def test_find_class_definitions(self) -> None:
        """Find all class definitions."""
        matches = search_in_file(
            PY_SOURCE,
            "(class_definition name: (identifier) @name) @cls",
            "python",
        )
        names = [m.captures.get("name", "") for m in matches]
        assert "Greeter" in names
        assert len(matches) == 1

    def test_match_has_line_column(self) -> None:
        """Each match reports 1-based line and column."""
        matches = search_in_file(
            PY_SOURCE,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        for m in matches:
            assert m.line >= 1
            assert m.column >= 1
            assert m.file_path == ""  # default

    def test_match_has_text(self) -> None:
        """Each match includes the source text."""
        matches = search_in_file(
            PY_SOURCE,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        for m in matches:
            assert len(m.text) > 0
            assert "def" in m.text

    def test_match_includes_captures(self) -> None:
        """Captures dict maps capture names to their text."""
        matches = search_in_file(
            PY_SOURCE,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        hello_match = next(m for m in matches if "hello" in m.captures.get("name", ""))
        assert hello_match.captures["name"] == "hello"
        assert "func" in hello_match.captures

    def test_no_matches_returns_empty(self) -> None:
        """No matches returns empty list."""
        matches = search_in_file(
            PY_SOURCE,
            "(import_statement) @import",
            "python",
        )
        assert matches == []

    def test_invalid_query_raises(self) -> None:
        """Invalid S-expression raises AstQueryError."""
        with pytest.raises(AstQueryError):
            search_in_file(PY_SOURCE, "not a valid query (((", "python")

    def test_file_path_is_preserved(self) -> None:
        """file_path parameter is stored in matches."""
        matches = search_in_file(
            PY_SOURCE,
            "(function_definition name: (identifier) @name) @func",
            "python",
            file_path="test.py",
        )
        for m in matches:
            assert m.file_path == "test.py"

    def test_search_python_call_expressions(self) -> None:
        """Find function calls and capture the callee name."""
        source = "print('hello')\nlen([1, 2, 3])\nresult = foo(bar())"
        matches = search_in_file(
            source,
            "(call function: (identifier) @callee) @call",
            "python",
        )
        callees = [m.captures.get("callee", "") for m in matches]
        assert "print" in callees
        assert "len" in callees
        assert "foo" in callees
        assert "bar" in callees


@requires_ast
class TestSearchInDirectory:
    """Recursive directory search."""

    def test_search_finds_files(self, tmp_path) -> None:
        """Finds matches across multiple .py files in a directory."""
        (tmp_path / "a.py").write_text("def foo(): pass\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def bar(): pass\n", encoding="utf-8")
        # This file should be skipped
        (tmp_path / "README.md").write_text("# Readme\n", encoding="utf-8")

        matches = search_in_directory(
            tmp_path,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        names = {m.captures.get("name", "") for m in matches}
        assert "foo" in names
        assert "bar" in names
        # All matches should have file paths
        for m in matches:
            assert m.file_path != ""

    def test_skips_hidden_dirs(self, tmp_path) -> None:
        """Skips __pycache__ and dot-directories."""
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("def secret(): pass\n", encoding="utf-8")

        matches = search_in_directory(
            tmp_path,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        names = {m.captures.get("name", "") for m in matches}
        assert "secret" not in names

    def test_skips_node_modules(self, tmp_path) -> None:
        """Skips node_modules directories."""
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "pkg.py").write_text("def should_skip(): pass\n", encoding="utf-8")

        matches = search_in_directory(
            tmp_path,
            "(function_definition name: (identifier) @name) @func",
            "python",
        )
        names = {m.captures.get("name", "") for m in matches}
        assert "should_skip" not in names


@requires_ast
class TestReplaceInFile:
    """Replace matched patterns in source text."""

    def test_replace_function_name(self) -> None:
        """Replace a captured node with new text."""
        source = "def old_name(): pass\n"
        new_source, count = replace_in_file(
            source,
            "(function_definition name: (identifier) @name) @func",
            "def new_name(): pass",
            "python",
        )
        assert count == 1
        assert "def new_name(): pass" in new_source
        assert "old_name" not in new_source

    def test_no_match_returns_original(self) -> None:
        """When nothing matches, source is unchanged and count is 0."""
        source = "x = 1\ny = 2\n"
        new_source, count = replace_in_file(
            source,
            "(function_definition) @func",
            "replaced",
            "python",
        )
        assert count == 0
        assert new_source == source

    def test_replace_preserves_unmatched_text(self) -> None:
        """Unmatched text is preserved exactly."""
        source = "# Header comment\ndef foo(): pass\n# Footer comment\n"
        new_source, count = replace_in_file(
            source,
            "(function_definition name: (identifier) @name) @func",
            "def replaced(): pass",
            "python",
        )
        assert count == 1
        assert "# Header comment" in new_source
        assert "# Footer comment" in new_source
        assert "def replaced(): pass" in new_source

    def test_invalid_query_raises(self) -> None:
        """Invalid query pattern raises AstQueryError."""
        with pytest.raises(AstQueryError):
            replace_in_file("x = 1", "((broken", "replacement", "python")

    def test_replace_multiple_occurrences(self) -> None:
        """Multiple matches are all replaced."""
        source = "def a(): pass\ndef b(): pass\n"
        new_source, count = replace_in_file(
            source,
            "(function_definition) @func",
            "def replaced(): pass",
            "python",
        )
        assert count == 2
        assert new_source.count("def replaced(): pass") == 2

    def test_returned_count_matches_actual(self) -> None:
        """The count return value is accurate."""
        source = "def one(): pass\ndef two(): pass\ndef three(): pass\n"
        _, count = replace_in_file(
            source,
            "(function_definition) @func",
            "replaced",
            "python",
        )
        assert count == 3


@requires_ast
class TestParser:
    """Parser creation and configuration."""

    def test_get_parser_returns_valid_parser(self) -> None:
        parser = _get_parser("python")
        assert parser is not None
        # Should be able to parse
        tree = parser.parse(b"x = 1")
        assert tree.root_node is not None

    def test_parser_language_is_set(self) -> None:
        """Parser has language configured after _get_parser."""
        parser = _get_parser("python")
        tree = parser.parse(b"def f(): pass")
        # Parsing Python should produce a module node
        assert tree.root_node.type == "module"
