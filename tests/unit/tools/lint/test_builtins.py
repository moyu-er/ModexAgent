"""Tests for tools.lint — FileLinter ABC, LintRegistry, RuffLinter, LintIssue/LintResult."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.tools.lint import (
    FileLinter,
    LintIssue,
    LintRegistry,
    LintResult,
    RuffLinter,
    default_lint_registry,
)

# ── LintIssue ───────────────────────────────────────────────────────────────


class TestLintIssue:
    """LintIssue is a frozen value object — only message + source required."""

    def test_minimal_issue_only_message_and_source(self) -> None:
        """Unstructured linter output: only message + source, rest defaults."""
        issue = LintIssue(message="something is wrong", source="shellcheck")
        assert issue.message == "something is wrong"
        assert issue.source == "shellcheck"
        assert issue.line == 0
        assert issue.column == 0
        assert issue.severity == "info"
        assert issue.code == ""

    def test_full_structured_issue(self) -> None:
        """Structured linter output: all fields populated."""
        issue = LintIssue(
            message="undefined name 'bar'",
            source="ruff",
            line=10,
            column=5,
            severity="error",
            code="F821",
        )
        assert issue.line == 10
        assert issue.severity == "error"
        assert issue.code == "F821"

    def test_issue_is_frozen(self) -> None:
        """LintIssue is immutable (frozen Pydantic)."""
        issue = LintIssue(message="x", source="ruff")
        with pytest.raises((TypeError, ValueError, Exception)):  # noqa: B017
            issue.message = "y"  # type: ignore[misc]


# ── LintResult ──────────────────────────────────────────────────────────────


class TestLintResult:
    """LintResult carries status + issues + optional message."""

    def test_ok_with_zero_issues(self) -> None:
        result = LintResult(status="ok")
        assert result.status == "ok"
        assert result.issues == []
        assert result.message == ""

    def test_ok_with_issues(self) -> None:
        issues = [LintIssue(message="x", source="ruff", line=1)]
        result = LintResult(status="ok", issues=issues)
        assert len(result.issues) == 1

    def test_unavailable(self) -> None:
        result = LintResult(status="unavailable", message="ruff not found in PATH")
        assert result.status == "unavailable"
        assert result.issues == []
        assert "not found" in result.message

    def test_result_is_frozen(self) -> None:
        result = LintResult(status="ok")
        with pytest.raises((TypeError, ValueError, Exception)):  # noqa: B017
            result.status = "error"  # type: ignore[misc]


# ── FileLinter ABC ──────────────────────────────────────────────────────────


class TestFileLinterABC:
    """FileLinter is an ABC — cannot be instantiated directly."""

    def test_cannot_instantiate_abc(self) -> None:
        """FileLinter is abstract; direct instantiation raises TypeError."""
        with pytest.raises(TypeError):
            FileLinter()  # type: ignore[abstract]

    def test_subclass_must_implement_all_abstract_methods(self) -> None:
        """Missing abstract method implementation raises TypeError on init."""

        class IncompleteLinter(FileLinter):
            @property
            def name(self) -> str:
                return "incomplete"

        with pytest.raises(TypeError):
            IncompleteLinter()  # type: ignore[abstract]


# ── LintRegistry ────────────────────────────────────────────────────────────


class _StubLinter(FileLinter):
    """Test-double linter with configurable supports + results."""

    def __init__(
        self,
        linter_name: str,
        supports_suffixes: list[str],
        result: LintResult | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._name = linter_name
        self._suffixes = supports_suffixes
        self._result = result or LintResult(status="ok")
        self._raise = raise_exc
        self.lint_call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def supports(self, path: Path) -> bool:
        return path.suffix in self._suffixes

    async def lint(self, path: Path) -> LintResult:
        self.lint_call_count += 1
        if self._raise is not None:
            raise self._raise
        return self._result


class TestLintRegistry:
    """Registry selects all matching linters, runs them concurrently, merges."""

    def test_empty_registry_returns_empty_for_any_file(self) -> None:
        """No linters registered → lint_file returns empty list (no match)."""
        reg = LintRegistry()
        result = reg.lint_file(Path("foo.md"))
        assert result == []

    def test_single_match(self) -> None:
        """One linter matches → runs it, returns its issues."""
        reg = LintRegistry()
        issue = LintIssue(message="bad", source="stub", line=1)
        reg.register(
            _StubLinter("stub", [".py"], LintResult(status="ok", issues=[issue]))
        )
        result = reg.lint_file(Path("test.py"))
        assert len(result) == 1
        assert result[0].source == "stub"

    def test_multi_match_all_run(self) -> None:
        """Two linters both support .py → both run, results merged."""
        reg = LintRegistry()
        reg.register(
            _StubLinter(
                "ruff", [".py"], LintResult(
                    status="ok",
                    issues=[LintIssue(message="r1", source="ruff", line=5)],
                )
            )
        )
        reg.register(
            _StubLinter(
                "mypy", [".py"], LintResult(
                    status="ok",
                    issues=[LintIssue(message="m1", source="mypy", line=2)],
                )
            )
        )
        result = reg.lint_file(Path("test.py"))
        assert len(result) == 2
        sources = {i.source for i in result}
        assert sources == {"ruff", "mypy"}

    def test_multi_match_sorted_by_line(self) -> None:
        """Merged issues sorted by (line, column); line=0 sorts last."""
        reg = LintRegistry()
        reg.register(
            _StubLinter(
                "ruff", [".py"], LintResult(status="ok", issues=[
                    LintIssue(message="r10", source="ruff", line=10, column=3),
                    LintIssue(message="r0", source="ruff", line=0),  # no line → last
                ])
            )
        )
        reg.register(
            _StubLinter(
                "mypy", [".py"], LintResult(status="ok", issues=[
                    LintIssue(message="m5", source="mypy", line=5, column=1),
                ])
            )
        )
        result = reg.lint_file(Path("test.py"))
        # Expected order: line 5 (mypy), line 10 (ruff), line 0 (ruff)
        assert [i.message for i in result] == ["m5", "r10", "r0"]

    def test_no_match_returns_empty(self) -> None:
        """File suffix not supported by any linter → empty list."""
        reg = LintRegistry()
        reg.register(_StubLinter("ruff", [".py"]))
        result = reg.lint_file(Path("readme.md"))
        assert result == []

    def test_one_linter_crashes_others_still_run(self) -> None:
        """fail-open per linter: if one crashes, others' results survive."""
        reg = LintRegistry()
        reg.register(
            _StubLinter("bad", [".py"], raise_exc=RuntimeError("boom"))
        )
        reg.register(
            _StubLinter(
                "good", [".py"], LintResult(
                    status="ok",
                    issues=[LintIssue(message="survived", source="good", line=1)],
                )
            )
        )
        result = reg.lint_file(Path("test.py"))
        assert len(result) == 1
        assert result[0].source == "good"

    def test_linter_names_property(self) -> None:
        """linter_names lists all registered linter names."""
        reg = LintRegistry()
        reg.register(_StubLinter("ruff", [".py"]))
        reg.register(_StubLinter("mypy", [".py"]))
        assert reg.linter_names == ["ruff", "mypy"]


# ── RuffLinter ──────────────────────────────────────────────────────────────


class TestRuffLinter:
    """RuffLinter supports .py files, parses concise output, fail-open."""

    def test_supports_py_only(self) -> None:
        """RuffLinter.supports True for .py, False for others."""
        linter = RuffLinter()
        assert linter.supports(Path("foo.py")) is True
        assert linter.supports(Path("foo.ts")) is False
        assert linter.supports(Path("foo.md")) is False

    def test_name_is_ruff(self) -> None:
        assert RuffLinter().name == "ruff"

    @pytest.mark.asyncio
    async def test_lint_clean_file_returns_zero_issues(self, tmp_path: Path) -> None:
        """A valid Python file → status ok, zero issues."""
        py = tmp_path / "clean.py"
        py.write_text("x = 1\n", encoding="utf-8")
        linter = RuffLinter()
        result = await linter.lint(py)
        assert result.status == "ok"
        assert result.issues == []

    @pytest.mark.asyncio
    async def test_lint_file_with_undefined_name(self, tmp_path: Path) -> None:
        """A Python file with F821 → structured issue with line/column/code."""
        py = tmp_path / "bad.py"
        py.write_text("print(undefined_var)\n", encoding="utf-8")
        linter = RuffLinter()
        result = await linter.lint(py)
        assert result.status == "ok"
        assert len(result.issues) >= 1
        issue = result.issues[0]
        assert issue.source == "ruff"
        assert issue.line >= 1
        assert issue.code != ""  # ruff produces a rule code (e.g. F821)

    @pytest.mark.asyncio
    async def test_lint_nonexistent_file_fail_open(self, tmp_path: Path) -> None:
        """Nonexistent file → fail-open (status unavailable or ok with 0 issues)."""
        py = tmp_path / "ghost.py"
        linter = RuffLinter()
        result = await linter.lint(py)
        # fail-open: either unavailable (ruff reports error) or ok with 0 issues
        assert result.status in ("ok", "unavailable", "error")
        assert result.issues == []


# ── default_lint_registry ───────────────────────────────────────────────────


class TestDefaultLintRegistry:
    """The module-level default registry comes with RuffLinter pre-registered."""

    def test_default_registry_has_ruff(self) -> None:
        """default_lint_registry has at least RuffLinter registered."""
        assert "ruff" in default_lint_registry.linter_names

    def test_default_registry_lints_python(self, tmp_path: Path) -> None:
        """default_lint_registry.lint_file on a .py file produces results."""
        py = tmp_path / "x.py"
        py.write_text("x = 1\n", encoding="utf-8")
        result = default_lint_registry.lint_file(py)
        # Clean file → empty list (no issues). We just verify no crash.
        assert isinstance(result, list)

    def test_default_registry_skips_txt(self) -> None:
        """default_lint_registry.lint_file on a .txt file → empty (no linter registered for .txt)."""
        result = default_lint_registry.lint_file(Path("readme.txt"))
        assert result == []

    def test_default_registry_has_nine_linters(self) -> None:
        """default_lint_registry includes ruff + 8 built-in linters."""
        names = default_lint_registry.linter_names
        assert "ruff" in names
        assert "mypy" in names
        assert "biome" in names
        assert "shellcheck" in names
        assert "golangci-lint" in names
        assert "clippy" in names
        assert "yamllint" in names
        assert "markdownlint" in names
        assert "pmd" in names
