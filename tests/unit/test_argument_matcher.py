"""Tests for ArgumentMatcher path resolution and matching."""
from pathlib import Path

import pytest

from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher


class TestResolvePath:
    """Test _resolve_path handles ., ~, and absolute paths."""

    def test_dot_resolves_to_project_root(self):
        root = Path("/project")
        matcher = ArgumentMatcher(project_root=root)
        result = matcher._resolve_path(".")
        assert result == root

    def test_dot_slash_resolves_to_project_root_subpath(self):
        root = Path("/project")
        matcher = ArgumentMatcher(project_root=root)
        result = matcher._resolve_path("./data")
        assert result == Path("/project/data")

    def test_tilde_resolves_to_home(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        result = matcher._resolve_path("~/Documents")
        assert result == Path.home() / "Documents"

    def test_absolute_path_unchanged(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        result = matcher._resolve_path("/etc/passwd")
        assert result == Path("/etc/passwd")


class TestMatchAny:
    """Test _match_any with fnmatch patterns."""

    def test_star_matches_any(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/any/path"), ["*"]) is True

    def test_specific_pattern_matches(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/project/data"), ["/project/*"]) is True

    def test_no_match_returns_false(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/outside"), ["/project/*"]) is False

    def test_multiple_patterns_or_logic(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        assert matcher._match_any(Path("/tmp"), ["/project/*", "/tmp"]) is True


class TestMatches:
    """Test matches() with tool arguments."""

    def test_path_in_allowed_paths(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"path": "./file.txt"}
        assert matcher.matches(args, ["./*"]) is True

    def test_path_not_in_allowed_paths(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"path": "/etc/passwd"}
        assert matcher.matches(args, ["./*"]) is False

    def test_no_path_argument_returns_true(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"content": "hello"}
        assert matcher.matches(args, ["./*"]) is True

    def test_working_dir_argument_extracted(self):
        matcher = ArgumentMatcher(project_root=Path("/project"))
        args = {"command": "ls", "working_dir": "./scripts"}
        assert matcher.matches(args, ["./*"]) is True
