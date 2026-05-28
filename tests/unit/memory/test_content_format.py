"""Tests for ChatMessage content_format, truncatable_paths, created_at extensions."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from framework.memory.core.message import ChatMessage, ContentFormat


def test_content_format_default_is_plain():
    msg = ChatMessage(role="user", content="hello")
    assert msg.content_format == ContentFormat.PLAIN


def test_content_format_xml():
    msg = ChatMessage(
        role="user",
        content="<msg>hi</msg>",
        content_format=ContentFormat.XML,
    )
    assert msg.content_format == ContentFormat.XML


def test_truncatable_paths_default_none():
    msg = ChatMessage(role="user", content="hello")
    assert msg.truncatable_paths is None


def test_truncatable_paths_xml():
    msg = ChatMessage(
        role="user",
        content="<msg><content>x</content></msg>",
        content_format=ContentFormat.XML,
        truncatable_paths=["content"],
    )
    assert msg.truncatable_paths == ["content"]


def test_created_at_default_none():
    msg = ChatMessage(role="user", content="hello")
    assert msg.created_at is None


def test_created_at_set():
    ts = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
    msg = ChatMessage(role="user", content="hello", created_at=ts)
    assert msg.created_at == ts


def test_coerce_preserves_content_format():
    msg = ChatMessage.coerce({
        "role": "user",
        "content": "<msg>hi</msg>",
        "content_format": "xml",
        "truncatable_paths": ["content"],
        "created_at": "2026-05-28 14:30:00",
    })
    assert msg.content_format == ContentFormat.XML
    assert msg.truncatable_paths == ["content"]
    assert msg.created_at is not None
    assert msg.created_at.year == 2026


def test_to_dict_serializes_new_fields():
    ts = datetime(2026, 5, 28, 14, 30, 0, tzinfo=timezone.utc)
    msg = ChatMessage(
        role="user",
        content="<msg>hi</msg>",
        content_format=ContentFormat.XML,
        truncatable_paths=["content"],
        created_at=ts,
    )
    d = msg.to_dict()
    assert d["content_format"] == "xml"
    assert d["truncatable_paths"] == ["content"]
    assert d["created_at"] == "2026-05-28 14:30:00"


def test_to_dict_omits_defaults():
    """Plain messages should omit content_format and truncatable_paths from serialization."""
    msg = ChatMessage(role="user", content="hello")
    d = msg.to_dict()
    assert "content_format" not in d
    assert "truncatable_paths" not in d
    assert "created_at" not in d
