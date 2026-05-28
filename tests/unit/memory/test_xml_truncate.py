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
