"""tree-sitter pattern matching engine for AST search/replace.

Uses tree-sitter's S-expression query language for pattern matching.
Pattern syntax: tree-sitter query S-expressions with @capture_name for captures.

Examples:
  (function_definition name: (identifier) @name) @func
  (class_definition name: (identifier) @name) @cls
  (call function: (identifier) @callee arguments: (argument_list) @args)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Graceful degradation when tree_sitter is not installed ──

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


class AstParseError(RuntimeError):
    """Raised when source code fails to parse."""
    pass


class AstQueryError(RuntimeError):
    """Raised when a query pattern is invalid."""
    pass


# ── Language resolution ──

def _resolve_language(language: str) -> Any:
    """Get a tree-sitter Language object for the given language name.

    tree-sitter 0.24+ grammars return PyCapsule objects from .language();
    they must be wrapped with tree_sitter.Language().

    Raises:
        AstNotAvailableError: if tree-sitter or the grammar is not installed
    """
    if not _TREE_SITTER_AVAILABLE:
        raise AstNotAvailableError(AST_UNAVAILABLE_MSG)

    import tree_sitter

    lang_map: dict[str, Any] = {}
    if _TREE_SITTER_PYTHON_AVAILABLE:
        import tree_sitter_python
        lang_map["python"] = tree_sitter.Language(tree_sitter_python.language())
    if _TREE_SITTER_JAVA_AVAILABLE:
        import tree_sitter_java
        lang_map["java"] = tree_sitter.Language(tree_sitter_java.language())

    lang = lang_map.get(language)
    if lang is None:
        available = list(lang_map.keys())
        raise AstNotAvailableError(
            f"Language '{language}' not available. Available: {available}. "
            f"Install: pip install tree-sitter-{language}"
        )
    return lang


def _get_parser(language: str) -> Any:
    """Get a tree-sitter Parser configured for the given language.

    Raises:
        AstNotAvailableError: if tree-sitter or the grammar is not installed
    """
    import tree_sitter

    lang = _resolve_language(language)
    parser = tree_sitter.Parser()
    parser.language = lang
    return parser


# ── File extension mapping ──

_EXT_MAP: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "java": (".java",),
}


# ── AST Match dataclass ──

@dataclass
class AstMatch:
    """A single AST pattern match in a source file."""
    file_path: str
    line: int
    column: int
    text: str
    captures: dict[str, str] = field(default_factory=dict)


# ── Core engine functions ──

def _node_text(node: Any) -> str:
    """Extract text from a tree-sitter node, handling bytes/str."""
    text = node.text
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


def search_in_file(
    source: str,
    pattern: str,
    language: str,
    file_path: str = "",
) -> list[AstMatch]:
    """Search for AST pattern matches in a source string.

    Uses tree-sitter's S-expression query language with @capture syntax.
    Example pattern: (function_definition name: (identifier) @name) @func

    Each match returns the outermost node of each pattern match with all
    named captures as metadata.

    Raises:
        AstNotAvailableError: if tree-sitter is not installed
        AstParseError: if source fails to parse
        AstQueryError: if the query pattern is invalid
    """
    import tree_sitter

    parser = _get_parser(language)
    lang = _resolve_language(language)

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    # Check for parse errors (ERROR nodes at top level)
    if root.has_error:
        logger.warning("Parse error in source file: %s", file_path or "<string>")

    try:
        query = tree_sitter.Query(lang, pattern)
    except tree_sitter.QueryError as e:
        raise AstQueryError(f"Invalid query pattern: {e}") from e

    cursor = tree_sitter.QueryCursor(query)
    matches: list[AstMatch] = []

    for _pattern_idx, match in cursor.matches(root):
        # Find the outermost node in this match (largest byte range)
        outermost_node: Any | None = None
        outermost_start = 1 << 60
        outermost_end = 0
        captures: dict[str, str] = {}

        for capture_name, nodes in match.items():
            for node in nodes:
                captures[capture_name] = _node_text(node)
                if node.start_byte < outermost_start:
                    outermost_start = node.start_byte
                if node.end_byte > outermost_end:
                    outermost_end = node.end_byte
                if node.start_byte <= outermost_start and node.end_byte >= outermost_end:
                    outermost_node = node

        if outermost_node is not None:
            start = outermost_node.start_point
            matches.append(AstMatch(
                file_path=file_path,
                line=start[0] + 1,   # 0-based → 1-based
                column=start[1] + 1,  # 0-based → 1-based
                text=_node_text(outermost_node),
                captures=captures,
            ))

    return matches


def search_in_directory(
    directory: Path,
    pattern: str,
    language: str,
    file_extensions: tuple[str, ...] | None = None,
) -> list[AstMatch]:
    """Search for AST pattern matches across files in a directory.

    Args:
        directory: Root directory to search.
        pattern: tree-sitter S-expression query pattern.
        language: "python" or "java".
        file_extensions: File extensions to scan. Defaults to language-appropriate list.

    Returns:
        List of AstMatch objects across all scanned files.
    """
    exts = file_extensions or _EXT_MAP.get(language, (".py",))
    results: list[AstMatch] = []

    for file_path in directory.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix not in exts:
            continue
        # Skip common non-project directories
        parts = file_path.parts
        if any(p.startswith(".") or p in ("node_modules", "__pycache__", "venv", ".venv") for p in parts):
            continue

        try:
            source = file_path.read_text(encoding="utf-8")
            results.extend(search_in_file(source, pattern, language, str(file_path)))
        except (AstNotAvailableError, AstQueryError):
            raise
        except Exception:
            logger.debug("Failed to search %s", file_path, exc_info=True)

    return results


def replace_in_file(
    source: str,
    pattern: str,
    replacement: str,
    language: str,
) -> tuple[str, int]:
    """Replace AST pattern matches in source text.

    Each match of `pattern` is replaced with `replacement`.
    Matches are sorted by position and replaced from end to start
    to preserve byte offsets.

    Args:
        source: Source code string.
        pattern: tree-sitter S-expression query pattern.
        replacement: Text to replace each match with.
        language: "python" or "java".

    Returns:
        (new_source, replacement_count) tuple.

    Raises:
        AstNotAvailableError: if tree-sitter is not installed
        AstParseError: if source fails to parse
        AstQueryError: if the query pattern is invalid
    """
    import tree_sitter

    parser = _get_parser(language)
    lang = _resolve_language(language)

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    try:
        query = tree_sitter.Query(lang, pattern)
    except tree_sitter.QueryError as e:
        raise AstQueryError(f"Invalid query pattern: {e}") from e

    cursor = tree_sitter.QueryCursor(query)
    matches = cursor.matches(root)

    if not matches:
        return source, 0

    # Collect byte ranges of outermost nodes from each match
    replacements: list[tuple[int, int]] = []
    for _pattern_idx, match in matches:
        min_start = 1 << 60
        max_end = 0
        for _capture_name, nodes in match.items():
            for node in nodes:
                if node.start_byte < min_start:
                    min_start = node.start_byte
                if node.end_byte > max_end:
                    max_end = node.end_byte
        if min_start < max_end:
            replacements.append((min_start, max_end))

    # Deduplicate and sort reverse (end→start) to preserve byte offsets
    replacements = sorted(set(replacements), key=lambda x: x[0], reverse=True)

    result_bytes = source_bytes
    for start, end in replacements:
        result_bytes = result_bytes[:start] + replacement.encode("utf-8") + result_bytes[end:]

    return result_bytes.decode("utf-8"), len(replacements)
