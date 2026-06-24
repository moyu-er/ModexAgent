from __future__ import annotations

import pytest

from modex_agent.sandbox.guard_traversal import PathTraversalConfig, PathTraversalGuard


class TestPathTraversalGuard:
    """Path traversal detection: ../ and ..\\ in command strings."""

    def test_posix_traversal_blocked(self) -> None:
        guard = PathTraversalGuard()
        result = guard.check("cat ../../../etc/passwd")
        assert not result.allowed
        assert any(m.category == "path_traversal" for m in result.matches)

    def test_windows_traversal_blocked(self) -> None:
        guard = PathTraversalGuard()
        result = guard.check("type ..\\..\\secrets.txt")
        assert not result.allowed
        assert any(m.category == "path_traversal" for m in result.matches)

    def test_traversal_disabled_by_config(self) -> None:
        config = PathTraversalConfig(enabled=False)
        guard = PathTraversalGuard(config)
        result = guard.check("cat ../../../etc/passwd")
        assert result.allowed

    def test_safe_double_dot_not_traversal(self) -> None:
        """Double dots inside a filename are not traversal."""
        guard = PathTraversalGuard()
        result = guard.check("cat file..name.txt")
        assert result.allowed

    def test_single_dot_allowed(self) -> None:
        """Single dot paths are fine."""
        guard = PathTraversalGuard()
        result = guard.check("cat ./file.txt")
        assert result.allowed
