"""Tests for XML-safe truncation."""
from __future__ import annotations

import pytest

from framework.memory.xml_truncate import truncate_xml_safe


XML_SHORT = "<msg><content>hello</content></msg>"

XML_LONG = """<agent_message source="planner" timestamp="2026-05-28 14:30:00">
  <thinking>需要查询数据</thinking>
  <content>""" + ("x" * 500) + """</content>
  <status>ok</status>
</agent_message>"""

XML_MALFORMED = "<msg><content>hello</content>"


def test_short_xml_unchanged():
    result = truncate_xml_safe(XML_SHORT, max_chars=200, truncatable_paths=["content"])
    assert result == XML_SHORT


def test_truncates_only_content_element():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=["content"])
    assert '<agent_message source="planner"' in result
    assert '<thinking>需要查询数据</thinking>' in result
    assert '<status>ok</status>' in result
    assert '</agent_message>' in result
    # Verify the truncatable field text was cut to <= max_chars
    content_text = result.split('<content>', 1)[1].split('</content>', 1)[0]
    assert len(content_text) <= 200


def test_preserves_attributes():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=["content"])
    assert 'source="planner"' in result
    assert 'timestamp="2026-05-28 14:30:00"' in result


def test_fallback_malformed_xml():
    result = truncate_xml_safe(XML_MALFORMED, max_chars=20, truncatable_paths=["content"])
    assert result.startswith("<msg><content>hel")
    assert "</content>" in result
    assert "</msg>" in result
    assert "Content truncated" in result


def test_no_truncatable_paths_preserves_structure():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=[])
    assert len(result) <= 280
    assert '<agent_message' in result
    assert '</agent_message>' in result


def test_none_truncatable_paths_fallback():
    result = truncate_xml_safe(XML_LONG, max_chars=200, truncatable_paths=None)
    assert len(result) <= 280
    assert '</agent_message>' in result


# ── Nested field: skill command XML ──────────────────────────────────────
# Skill XML has <skill> nested inside <command_context> and <user_input>
# as a sibling.  truncatable_paths uses recursive iter() so nested
# elements are found regardless of depth.


def _make_skill_xml(
    skill_content: str = "",
    user_content: str = "",
    *,
    skill_name: str = "weather",
) -> str:
    return (
        f'<command_context type="skill" name="{skill_name}">\n'
        f"<skill>\n{skill_content}\n</skill>\n"
        f"</command_context>\n\n"
        f"<user_input>\n{user_content}\n</user_input>"
    )


def test_skill_xml_nested_skill_found_via_recursive_iter():
    """Prove root.iter('skill') finds <skill> inside <command_context>.

    Uses realistic URB truncation budget (4000 chars, the default
    max_user_chars).  Skill content is 5000 chars so truncation triggers.
    """
    xml = _make_skill_xml(skill_content="x" * 5000, user_content="short question")
    paths = ["skill"]

    result = truncate_xml_safe(xml, max_chars=4000, truncatable_paths=paths)

    assert "<skill>" in result
    assert "</skill>" in result
    assert "<user_input>" in result
    assert len(result) < len(xml)


def test_skill_xml_multi_path_nested_and_sibling():
    """Both nested (<skill>) and sibling (<user_input>) found via iter."""
    xml = _make_skill_xml(skill_content="a" * 5000, user_content="b" * 3000)
    paths = ["skill", "user_input"]

    result = truncate_xml_safe(xml, max_chars=4000, truncatable_paths=paths)

    assert "<skill>" in result
    assert "<user_input>" in result
    assert len(result) < len(xml)


def test_skill_xml_truncates_user_input():
    """<user_input> (sibling, not nested) is also found."""
    skill_content = "short"
    user_input = "y" * 3000
    xml = _make_skill_xml(skill_content=skill_content, user_content=user_input)
    paths = ["user_input", "skill"]

    result = truncate_xml_safe(xml, max_chars=600, truncatable_paths=paths)

    # Both elements found and tags preserved
    assert "<skill>" in result
    assert "</skill>" in result
    assert "<user_input>" in result
    assert "</user_input>" in result
    assert len(result) < len(xml)


def test_skill_xml_preserves_command_context_attributes():
    """Attributes on <command_context> survive XML-structured truncation."""
    xml = _make_skill_xml(skill_content="z" * 4000, skill_name="test-skill")
    paths = ["user_input", "skill"]

    result = truncate_xml_safe(xml, max_chars=900, truncatable_paths=paths)

    assert 'type="skill"' in result
    assert 'name="test-skill"' in result


def test_truncatable_paths_missing_large_content_still_leaf_truncated():
    """When truncatable_paths don't cover the large content, Phase 2
    leaf-element truncation catches it.  Non-listed leaf elements whose
    text exceeds max_chars are truncated — preventing obscenely large
    single elements from surviving untouched.

    This reproduces the real-world case: content_format='xml' with
    truncatable_paths=['user_input'], but the real large content is
    inside <skill> which is NOT in the paths list.  <skill> is a leaf
    element (no child elements), so Phase 2 must catch it.
    """
    xml = _make_skill_xml(
        skill_content="x" * 30000,    # large content NOT in paths — leaf
        user_content="short question",  # small content IS in paths
    )
    paths = ["user_input"]

    result = truncate_xml_safe(xml, max_chars=4000, truncatable_paths=paths)

    # <skill> was a leaf with 30KB text — must have been truncated
    assert len(result) < len(xml), (
        f"Leaf-element safety net should have truncated <skill> text"
    )
    # No single leaf element should exceed max_chars
    assert "x" * 4000 not in result, (
        "No leaf element should have text > max_chars"
    )


def test_phase2_only_truncates_leaf_elements_not_parents():
    """Phase 2 must ONLY truncate leaf elements (no child elements).
    Parent elements containing nested XML children are never touched —
    this guarantees XML structure is never destroyed.

    Uses a path that matches a small field but misses the large leaf
    element, so Phase 1 runs (text_nodes non-empty) and Phase 2 catches
    the unlisted large leaf.
    """
    xml = (
        "<root>"
        "<small_field>tiny</small_field>"
        "<parent>"
        "<child1>short</child1>"
        "<child2>" + "x" * 8000 + "</child2>"
        "</parent>"
        "</root>"
    )
    # Paths include small_field but NOT child2 — Phase 1 truncates
    # small_field (no-op, it's short), Phase 2 catches child2 leaf.
    result = truncate_xml_safe(xml, max_chars=200, truncatable_paths=["small_field"])

    # XML structure must be intact — parent not touched
    assert result.count("<parent>") == result.count("</parent>"), "parent tags mismatch"
    assert "<child1>short</child1>" in result, "short child should be untouched"
    assert result.count("<child2>") == result.count("</child2>"), "child2 tags mismatch"
    # child2 text was truncated (leaf, 8000 chars > 200)
    assert len(result) < len(xml), "Phase 2 should have truncated child2"
    assert "x" * 8000 not in result


def test_empty_paths_still_bounded_via_boundary():
    """Empty truncatable_paths hits boundary fallback (no text_nodes → early
    return via _truncate_xml_boundary).  Result is cut at max_chars."""
    xml = _make_skill_xml(
        skill_content="y" * 10000,
        user_content="z" * 5000,
    )

    result = truncate_xml_safe(xml, max_chars=2000, truncatable_paths=[])

    # Boundary truncation cuts at max_chars (<r> wrapper was not used
    # here, so no unwrap overhead)
    assert len(result) <= 2300, (
        f"Boundary truncation with empty paths should be bounded, got {len(result)}"
    )


from framework.memory.tags import UrbTag

# ── List elements: governance URB XML ────────────────────────────────────
# The governance injection assembles <recent_messages> with multiple <entry>
# wrappers.  truncatable_paths uses recursive iter() so <user> and <you>
# inside every <entry> are found.


def _make_urb_xml(*entries: str) -> str:
    ct = UrbTag.CONTAINER.value
    et = UrbTag.ENTRY.value
    lines = [f"<{ct}>"]
    for i, body in enumerate(entries):
        role_attr = ""
        if i % 2 == 0:
            role_attr = ' role="agent"'
        lines.append(f"  <{et}{role_attr}>")
        lines.append(body)
        lines.append(f"  </{et}>")
    lines.append(f"</{ct}>")
    return "\n".join(lines)


def _urb_entry_body(user_content: str, completing: str | None = None) -> str:
    ut = UrbTag.USER_MSG.value
    yt = UrbTag.YOU_RESPONSE.value
    body = f"    <{ut}>{user_content}</{ut}>"
    if completing:
        body += f"\n    <{yt}>{completing}</{yt}>"
    return body


def test_urb_xml_finds_nested_fields_across_multiple_entries():
    """<user> inside every <entry> is found via recursive iter."""
    xml = _make_urb_xml(
        _urb_entry_body("u" * 3000),
        _urb_entry_body("v" * 3000, "a" * 2000),
        _urb_entry_body("w" * 3000),
    )
    paths = [UrbTag.USER_MSG.value, UrbTag.YOU_RESPONSE.value]

    result = truncate_xml_safe(xml, max_chars=600, truncatable_paths=paths)

    ct = UrbTag.CONTAINER.value
    et = UrbTag.ENTRY.value
    ut = UrbTag.USER_MSG.value
    yt = UrbTag.YOU_RESPONSE.value
    assert result.count(f"<{ct}>") == 1
    assert result.count(f"</{ct}>") == 1
    assert result.count(f"<{et}") == 3
    assert result.count(f"</{et}>") == 3
    assert result.count(f"<{ut}>") == result.count(f"</{ut}>")
    assert result.count(f"<{yt}>") == result.count(f"</{yt}>")
    assert result.count(f"<{ut}>") == 3
    assert len(result) < len(xml)


def test_urb_xml_preserves_entry_role_attributes():
    """Entry attributes like role='agent' survive truncation."""
    xml = _make_urb_xml(
        _urb_entry_body("a" * 4000),
        _urb_entry_body("b" * 4000, "c" * 4000),
    )
    paths = [UrbTag.USER_MSG.value, UrbTag.YOU_RESPONSE.value]

    result = truncate_xml_safe(xml, max_chars=500, truncatable_paths=paths)

    assert 'role="agent"' in result
    assert result.count(f"<{UrbTag.ENTRY.value}") == 2


def test_urb_xml_per_element_independent_truncation():
    """Each truncatable element gets its own max_chars — no budget split."""
    xml = _make_urb_xml(
        _urb_entry_body("p" * 4000),
        _urb_entry_body("q" * 4000, "r" * 4000),
    )
    paths = [UrbTag.USER_MSG.value, UrbTag.YOU_RESPONSE.value]

    result = truncate_xml_safe(xml, max_chars=800, truncatable_paths=paths)

    ut = UrbTag.USER_MSG.value
    yt = UrbTag.YOU_RESPONSE.value
    assert len(result) < len(xml)
    assert result.count(f"<{ut}>") == 2
    assert result.count(f"<{yt}>") == 1
    # All elements are present in the output — none starved


def test_urb_xml_mixed_completed_unfinished():
    """Entries without completing response only contribute user text."""
    xml = _make_urb_xml(
        _urb_entry_body("long_q" * 500),
        _urb_entry_body("done_q" * 500, "answer" * 500),
    )
    paths = [UrbTag.USER_MSG.value, UrbTag.YOU_RESPONSE.value]

    result = truncate_xml_safe(xml, max_chars=400, truncatable_paths=paths)

    ut = UrbTag.USER_MSG.value
    yt = UrbTag.YOU_RESPONSE.value
    et = UrbTag.ENTRY.value
    ct = UrbTag.CONTAINER.value
    assert result.count(f"<{ut}>") == 2
    assert result.count(f"<{yt}>") == 1
    for tag in [ut, yt, et, ct]:
        assert result.count(f"<{tag}") == result.count(f"</{tag}>"), (
            f"Mismatched {tag} tags"
        )


# ── truncate_for_archive tests ─────────────────────────────────────────


def test_truncate_for_archive_xml_content() -> None:
    """XML content is truncated preserving structure."""
    from framework.memory.xml_truncate import truncate_for_archive

    xml_content = "<root><data>" + ("x" * 2000) + "</data></root>"
    result = truncate_for_archive(xml_content, max_chars=500)

    assert "<root>" in result
    assert "</root>" in result
    assert "<!-- truncated for archive -->" in result
    assert len(result) <= 600  # Allow overhead for tags + comment


def test_truncate_for_archive_plain_text() -> None:
    """Plain text uses proportional head+tail truncation."""
    from framework.memory.xml_truncate import truncate_for_archive

    text = "a" * 2000
    result = truncate_for_archive(text, max_chars=1200)

    assert "truncated" in result
    assert len(result) <= 1300  # Allow overhead for marker
    # Head is 67%: 1200 * 0.67 = 804 chars
    assert result.startswith("a" * 800)


def test_truncate_for_archive_short_content_unchanged() -> None:
    """Content under max_chars is returned unchanged."""
    from framework.memory.xml_truncate import truncate_for_archive

    text = "short content"
    result = truncate_for_archive(text, max_chars=1200)
    assert result == text
