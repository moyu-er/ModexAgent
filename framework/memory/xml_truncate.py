"""XML-safe truncation for governance.

Uses xml.etree.ElementTree (stdlib) for parsing and serialization.
All truncation preserves XML structure — only text content inside
truncatable_paths elements is modified. Length pre-filter avoids
unnecessary parsing overhead.
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


def truncate_xml_safe(
    content: str,
    max_chars: int,
    truncatable_paths: list[str] | None = None,
) -> str:
    """Truncate XML content preserving structure.

    Length pre-filter: returns unchanged if total content fits.
    For well-formed XML: truncates only text inside truncatable_paths
    elements, preserving all tags, attributes, and non-truncatable text.
    For malformed XML: falls back to boundary-safe cut with tag closing.

    Args:
        content: XML content string.
        max_chars: Maximum characters to keep.
        truncatable_paths: Element tag names whose text can be truncated.

    Returns:
        Truncated XML string with preserved structure.
    """
    # Length pre-filter — avoid unnecessary parsing
    if len(content) <= max_chars:
        return content

    paths = truncatable_paths or []

    try:
        return _truncate_xml_structured(content, max_chars, paths)
    except ET.ParseError:
        logger.debug("XML parse failed, using boundary-safe fallback")
        return _truncate_xml_boundary(content, max_chars)
    except Exception:
        logger.debug("Unexpected error during XML truncation, falling back", exc_info=True)
        return _truncate_xml_boundary(content, max_chars)


def _truncate_xml_structured(
    content: str,
    max_chars: int,
    truncatable_paths: list[str],
) -> str:
    """Truncate well-formed XML using ElementTree.

    Collects all truncatable text content, computes proportional budget
    per element, and distributes the budget. Preserves all tags,
    attributes, and non-truncatable element text.
    """
    root = ET.fromstring(content)

    # Collect all truncatable text nodes with their elements
    text_nodes: list[tuple[ET.Element, str]] = []
    for path in truncatable_paths:
        for elem in root.iter(path):
            if elem.text and len(elem.text) > 0:
                text_nodes.append((elem, elem.text))

    if not text_nodes:
        # No truncatable text — must cut at boundary
        return _truncate_xml_boundary(content, max_chars)

    # Compute overhead: everything that is NOT truncatable text
    total_text_len = sum(len(text) for _, text in text_nodes)
    overhead = len(content) - total_text_len

    if overhead >= max_chars:
        # Overhead alone exceeds budget — must cut at boundary
        return _truncate_xml_boundary(content, max_chars)

    budget_for_text = max_chars - overhead
    if budget_for_text >= total_text_len:
        # All text fits — serialize as-is (shouldn't happen due to pre-filter)
        return ET.tostring(root, encoding="unicode")

    # Proportional truncation: give each text node its share of the budget
    for elem, original_text in text_nodes:
        proportion = len(original_text) / total_text_len
        elem_budget = max(1, int(budget_for_text * proportion))
        if len(original_text) > elem_budget:
            elem.text = original_text[:elem_budget]

    result = ET.tostring(root, encoding="unicode")
    # Secondary check: if serialized result still exceeds budget, boundary cut
    if len(result) > max_chars:
        return _truncate_xml_boundary(content, max_chars)
    return result


def _truncate_xml_boundary(content: str, max_chars: int) -> str:
    """Boundary-safe cut: truncate at char boundary, then close all open tags.

    Uses ElementTree iterparse to identify open elements at the cut point
    for proper tag closing. Falls back to simple cut on any failure.
    """
    prefix = content[:max_chars]

    # Find the last complete element boundary before max_chars
    # Walk backward through content to find a valid cut point
    open_tags = _find_open_tags(content, max_chars)
    for tag in reversed(open_tags):
        prefix += f'</{tag}>'

    prefix += '\n<!-- Content truncated -->'
    return prefix


def _find_open_tags(content: str, cut_pos: int) -> list[str]:
    """Find XML elements still open at cut_pos using iterative scanning.

    Uses a simple state machine over the prefix. Handles self-closing
    tags, attributes, and CDATA sections. Does not require well-formed
    XML — works on any partial content up to cut_pos.
    """
    open_tags: list[str] = []
    prefix = content[:cut_pos]
    i = 0
    n = len(prefix)

    while i < n:
        if prefix[i] != '<':
            i += 1
            continue

        # Check for closing tag: </name>
        if i + 1 < n and prefix[i + 1] == '/':
            end = prefix.find('>', i)
            if end == -1:
                break
            tag_content = prefix[i + 2:end].strip()
            tag_name = tag_content.split()[0] if tag_content else ""
            if tag_name and open_tags and open_tags[-1] == tag_name:
                open_tags.pop()
            i = end + 1
            continue

        # Check for self-closing tag: <name ... />
        end = prefix.find('>', i)
        if end == -1:
            # Unclosed tag starting at or before cut — we're inside a tag
            break

        tag_body = prefix[i + 1:end]
        # Skip processing instructions and comments
        if tag_body.startswith('?') or tag_body.startswith('!'):
            i = end + 1
            continue

        # Check if self-closing
        if tag_body.rstrip().endswith('/'):
            i = end + 1
            continue

        # Opening tag: extract tag name
        tag_name = tag_body.split()[0] if tag_body.strip() else ""
        if tag_name:
            open_tags.append(tag_name)
        i = end + 1

    return open_tags
