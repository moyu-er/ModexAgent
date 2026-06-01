"""Tests for AST engine graceful degradation."""

from __future__ import annotations

import pytest
from framework.tools.ast.engine import (
    is_ast_available,
    AstNotAvailableError,
    _get_parser,
)


class TestGracefulDegradation:
    """When tree-sitter is not installed, tools return helpful messages."""

    def test_is_ast_available_returns_bool(self) -> None:
        """is_ast_available() always returns a bool."""
        result = is_ast_available()
        assert isinstance(result, bool)

    def test_get_parser_invalid_language_raises(self) -> None:
        """Invalid language raises AstNotAvailableError."""
        with pytest.raises(AstNotAvailableError):
            _get_parser("nonexistent_language")
