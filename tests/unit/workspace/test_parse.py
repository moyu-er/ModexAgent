from pathlib import Path

import pytest

from framework.workspace.parse import parse_user_path


class TestParseUserPath:
    def test_absolute_path(self, tmp_path):
        target = tmp_path / "subdir"
        target.mkdir()
        result = parse_user_path(str(target), base=tmp_path)
        assert result == target.resolve()

    def test_relative_path(self, tmp_path):
        subdir = tmp_path / "project"
        subdir.mkdir()
        result = parse_user_path("project", base=tmp_path)
        assert result == subdir.resolve()

    def test_dot_dot_path(self, tmp_path):
        parent = tmp_path.parent
        result = parse_user_path("..", base=tmp_path)
        assert result == parent.resolve()

    def test_dot_path(self, tmp_path):
        result = parse_user_path(".", base=tmp_path)
        assert result == tmp_path.resolve()

    def test_tilde_expansion(self):
        result = parse_user_path("~/some_dir", base=Path("/tmp"))
        assert str(result).startswith(str(Path.home()))

    def test_forward_slash_subdir(self, tmp_path):
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        result = parse_user_path("a/b", base=tmp_path)
        assert result == subdir.resolve()

    def test_empty_path_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_user_path("", base=Path("/tmp"))

    def test_whitespace_only_path_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_user_path("   ", base=Path("/tmp"))

    def test_absolute_path_with_trailing_slash(self, tmp_path):
        result = parse_user_path(str(tmp_path) + "/", base=Path("/unrelated"))
        assert result == tmp_path.resolve()

    def test_backslash_as_path_separator(self, tmp_path):
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        result = parse_user_path("a\\b", base=tmp_path)
        assert result == subdir.resolve()

    def test_mixed_separators(self, tmp_path):
        subdir = tmp_path / "a" / "b" / "c"
        subdir.mkdir(parents=True)
        result = parse_user_path("a/b\\c", base=tmp_path)
        assert result == subdir.resolve()
