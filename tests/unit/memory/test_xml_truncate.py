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
    assert len(result) <= 250


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


# ── List elements: governance URB XML ────────────────────────────────────
# The governance injection assembles <pruned_conversation_context> with
# multiple <entry> wrappers.  truncatable_paths uses recursive iter() so
# <pruned_user_content> and <completing_assistant_content> inside every
# <entry> are found.


def _make_urb_xml(*entries: str) -> str:
    lines = ["<pruned_conversation_context>"]
    for i, body in enumerate(entries):
        role_attr = ""
        if i % 2 == 0:
            role_attr = ' role="agent"'
        lines.append(f"  <entry{role_attr}>")
        lines.append(body)
        lines.append("  </entry>")
    lines.append("</pruned_conversation_context>")
    return "\n".join(lines)


def _urb_entry_body(pruned_content: str, completing: str | None = None) -> str:
    body = f"    <pruned_user_content>{pruned_content}</pruned_user_content>"
    if completing:
        body += f"\n    <completing_assistant_content>{completing}</completing_assistant_content>"
    return body


def test_urb_xml_finds_nested_fields_across_multiple_entries():
    """<pruned_user_content> inside every <entry> is found via recursive iter."""
    xml = _make_urb_xml(
        _urb_entry_body("u" * 3000),
        _urb_entry_body("v" * 3000, "a" * 2000),
        _urb_entry_body("w" * 3000),
    )
    paths = ["pruned_user_content", "completing_assistant_content"]

    result = truncate_xml_safe(xml, max_chars=600, truncatable_paths=paths)

    assert result.count("<pruned_conversation_context>") == 1
    assert result.count("</pruned_conversation_context>") == 1
    assert result.count("<entry") == 3
    assert result.count("</entry>") == 3
    assert result.count("<pruned_user_content>") == result.count("</pruned_user_content>")
    assert result.count("<completing_assistant_content>") == result.count("</completing_assistant_content>")
    assert result.count("<pruned_user_content>") == 3
    assert len(result) < len(xml)


def test_urb_xml_preserves_entry_role_attributes():
    """Entry attributes like role='agent' survive truncation."""
    xml = _make_urb_xml(
        _urb_entry_body("a" * 4000),
        _urb_entry_body("b" * 4000, "c" * 4000),
    )
    paths = ["pruned_user_content", "completing_assistant_content"]

    result = truncate_xml_safe(xml, max_chars=500, truncatable_paths=paths)

    assert 'role="agent"' in result
    assert result.count("<entry") == 2


def test_urb_xml_proportional_distribution():
    """Budget is distributed proportionally across all truncatable text nodes."""
    xml = _make_urb_xml(
        _urb_entry_body("p" * 4000),
        _urb_entry_body("q" * 4000, "r" * 4000),
    )
    paths = ["pruned_user_content", "completing_assistant_content"]

    result = truncate_xml_safe(xml, max_chars=800, truncatable_paths=paths)

    assert len(result) < len(xml)
    assert len(result) <= 850
    assert result.count("<pruned_user_content>") == 2
    assert result.count("<completing_assistant_content>") == 1


def test_urb_xml_mixed_completed_unfinished():
    """Entries without completing_assistant_content only contribute user text."""
    xml = _make_urb_xml(
        _urb_entry_body("long_q" * 500),
        _urb_entry_body("done_q" * 500, "answer" * 500),
    )
    paths = ["pruned_user_content", "completing_assistant_content"]

    result = truncate_xml_safe(xml, max_chars=400, truncatable_paths=paths)

    assert result.count("<pruned_user_content>") == 2
    assert result.count("<completing_assistant_content>") == 1
    for tag in ["pruned_user_content", "entry", "pruned_conversation_context"]:
        assert result.count(f"<{tag}") == result.count(f"</{tag}>"), (
            f"Mismatched {tag} tags"
        )
