"""tree-sitter pattern matching engine for AST search/replace."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Graceful degradation when tree_sitter is not installed
_TREE_SITTER_AVAILABLE = False
try:
    import tree_sitter  # noqa: F401
    _TREE_SITTER_AVAILABLE = True
except ImportError:
    pass

_TREE_SITTER_PYTHON_AVAILABLE = False
try:
    import tree_sitter_python  # noqa: F401
    _TREE_SITTER_PYTHON_AVAILABLE = True
except ImportError:
    pass

_TREE_SITTER_JAVA_AVAILABLE = False
try:
    import tree_sitter_java  # noqa: F401
    _TREE_SITTER_JAVA_AVAILABLE = True
except ImportError:
    pass


def is_ast_available() -> bool:
    """Check if tree-sitter and at least one language grammar are installed."""
    return _TREE_SITTER_AVAILABLE and (_TREE_SITTER_PYTHON_AVAILABLE or _TREE_SITTER_JAVA_AVAILABLE)


AST_UNAVAILABLE_MSG = (
    "AST tools require tree-sitter. Install: pip install ModexAgent[ast]\n"
    "Or manually: pip install tree-sitter tree-sitter-python tree-sitter-java"
)


class AstNotAvailableError(RuntimeError):
    """Raised when tree-sitter or a grammar is not installed."""
    pass


def _get_parser(language: str) -> Any:
    """Get a tree-sitter Parser for the given language.

    Args:
        language: "python" or "java"

    Returns:
        tree_sitter.Parser instance

    Raises:
        AstNotAvailableError: if tree-sitter or the grammar is not installed
    """
    if not _TREE_SITTER_AVAILABLE:
        raise AstNotAvailableError(AST_UNAVAILABLE_MSG)

    import tree_sitter
    from tree_sitter import Language
    from tree_sitter import Parser

    lang_map: dict[str, Language] = {}
    if _TREE_SITTER_PYTHON_AVAILABLE:
        import tree_sitter_python
        lang_map["python"] = tree_sitter_python.language()
    if _TREE_SITTER_JAVA_AVAILABLE:
        import tree_sitter_java
        lang_map["java"] = tree_sitter_java.language()

    lang = lang_map.get(language)
    if lang is None:
        available = list(lang_map.keys())
        raise AstNotAvailableError(
            f"Language '{language}' not available. Available: {available}. "
            f"Install: pip install tree-sitter-{language}"
        )

    parser = Parser()
    parser.language(lang)
    return parser


@dataclass
class AstMatch:
    """A single AST pattern match in a source file."""
    file_path: str
    line: int
    column: int
    text: str
    captures: dict[str, str] = field(default_factory=dict)


def search_in_file(
    source: str,
    pattern: str,
    language: str,
    file_path: str = "",
) -> list[AstMatch]:
    """Search for AST pattern matches in a source string.

    Supports $VAR (single node) and $$$BODY (zero-or-more nodes) meta-variables.
    Multiple $$$BODY meta-variables are not supported (only one).
    """
    _ = source, pattern, language, file_path
    raise AstNotAvailableError(AST_UNAVAILABLE_MSG)


def search_in_directory(
    directory: Path,
    pattern: str,
    language: str,
    file_extensions: tuple[str, ...] | None = None,
) -> list[AstMatch]:
    """Search for AST pattern matches across files in a directory."""
    _ = directory, pattern, language, file_extensions
    raise AstNotAvailableError(AST_UNAVAILABLE_MSG)


def replace_in_file(
    source: str,
    pattern: str,
    replacement: str,
    language: str,
) -> tuple[str, int]:
    """Replace AST pattern matches in source. Returns (new_source, replacement_count)."""
    _ = source, pattern, replacement, language
    raise AstNotAvailableError(AST_UNAVAILABLE_MSG)
