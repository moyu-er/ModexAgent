"""Tests for UserBufferEntry and ScopedUserRetentionBuffer."""

from __future__ import annotations

import time

import pytest

from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.user_buffer import (
    ScopedUserRetentionBuffer,
    UserRetentionBufferConfig,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.registry.in_memory import InMemoryStoreRegistry
from framework.memory.user_buffer import UserBufferEntry


# ── UserBufferEntry tests ──


def test_entry_from_user_message():
    entry = UserBufferEntry.from_message(
        {"role": "user", "content": "hello"},
        pruned_at=time.time(),
    )
    assert entry.pruned_user_role == "user"
    assert entry.pruned_user_content == "hello"
    assert entry.completing_assistant_content is None
    assert not entry.is_completed


def test_entry_from_agent_message():
    entry = UserBufferEntry.from_message(
        {"role": "agent", "content": "task done", "source_agent": "planner"},
        pruned_at=time.time(),
    )
    assert entry.pruned_user_role == "agent"
    assert entry.pruned_user_source_agent == "planner"


def test_entry_roundtrip():
    entry = UserBufferEntry.from_message(
        {"role": "user", "content": "test"},
        pruned_at=time.time(),
    )
    data = entry.to_dict()
    restored = UserBufferEntry.from_dict(data)
    assert restored is not None
    assert restored.pruned_user_content == "test"
    assert restored.fingerprint == entry.fingerprint


def test_entry_fingerprint_identical():
    e1 = UserBufferEntry.from_message({"role": "user", "content": "same"}, pruned_at=1.0)
    e2 = UserBufferEntry.from_message({"role": "user", "content": "same"}, pruned_at=2.0)
    assert e1.fingerprint == e2.fingerprint


def test_entry_fingerprint_different():
    e1 = UserBufferEntry.from_message({"role": "user", "content": "a"}, pruned_at=1.0)
    e2 = UserBufferEntry.from_message({"role": "user", "content": "b"}, pruned_at=2.0)
    assert e1.fingerprint != e2.fingerprint


def test_entry_from_dict_invalid_role_returns_none():
    assert UserBufferEntry.from_dict({"pruned_user_role": "tool", "pruned_user_content": "x"}) is None


def test_entry_from_dict_missing_content_defaults_to_empty():
    """from_dict is lenient — missing fields get defaults."""
    entry = UserBufferEntry.from_dict({"pruned_user_role": "user"})
    assert entry is not None
    assert entry.pruned_user_content == ""


def test_entry_is_completed():
    entry = UserBufferEntry(
        pruned_user_role="user",
        pruned_user_content="q",
        pruned_user_source_agent=None,
        pruned_user_created_at=1.0,
        completing_assistant_content="answer",
        fingerprint="fp",
    )
    assert entry.is_completed


# ── ScopedUserRetentionBuffer tests ──


@pytest.fixture
def registry():
    return InMemoryStoreRegistry()


def _make_layer_set(registry: InMemoryStoreRegistry):
    return MemoryLayerFactory.single_user(registry=registry)


@pytest.mark.asyncio
async def test_urb_mark_all_completed(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-urb")

    # Add two unfinished entries
    e1 = UserBufferEntry.from_message({"role": "user", "content": "q1"}, pruned_at=time.time())
    e2 = UserBufferEntry.from_message({"role": "user", "content": "q2"}, pruned_at=time.time())
    assert layer_set.user_retention is not None
    await layer_set.user_retention.append_entries(ctx, [e1, e2])

    # Mark all completed
    await layer_set.user_retention.mark_all_completed(ctx, "assistant reply")

    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 2
    assert all(e.is_completed for e in entries)
    assert all(e.completing_assistant_content == "assistant reply" for e in entries)


@pytest.mark.asyncio
async def test_urb_fifo_eviction(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-fifo")

    for i in range(8):  # max_entries=5, so 3 should evict
        entry = UserBufferEntry.from_message(
            {"role": "user", "content": f"msg-{i}"},
            pruned_at=time.time(),
        )
        await layer_set.user_retention.upsert_pruned_user(ctx, entry)

    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 5
    contents = [e.pruned_user_content for e in entries]
    assert "msg-3" in contents  # oldest kept
    assert "msg-7" in contents  # newest kept
    assert "msg-0" not in contents  # evicted


@pytest.mark.asyncio
async def test_urb_dedup_replaces_unfinished(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-dedup")

    e1 = UserBufferEntry.from_message({"role": "user", "content": "dup"}, pruned_at=time.time())
    e2 = UserBufferEntry.from_message({"role": "user", "content": "unique"}, pruned_at=time.time())
    e3 = UserBufferEntry.from_message({"role": "user", "content": "dup"}, pruned_at=time.time())

    await layer_set.user_retention.upsert_pruned_user(ctx, e1)
    await layer_set.user_retention.upsert_pruned_user(ctx, e2)
    await layer_set.user_retention.upsert_pruned_user(ctx, e3)

    entries = await layer_set.user_retention.get_entries(ctx)
    # e1 should be replaced by e3, e2 stays
    assert len(entries) == 2
    contents = {e.pruned_user_content for e in entries}
    assert contents == {"unique", "dup"}


@pytest.mark.asyncio
async def test_urb_dedup_only_replaces_unfinished(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-dedup-completed")

    e1 = UserBufferEntry.from_message({"role": "user", "content": "dup"}, pruned_at=time.time())
    e2 = UserBufferEntry.from_message({"role": "user", "content": "dup"}, pruned_at=time.time())

    await layer_set.user_retention.upsert_pruned_user(ctx, e1)
    await layer_set.user_retention.mark_all_completed(ctx, "done")
    await layer_set.user_retention.upsert_pruned_user(ctx, e2)

    # e1 is completed, so e2 should NOT replace it — both should exist
    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 2  # completed e1 + unfinished e2


@pytest.mark.asyncio
async def test_urb_unfinished_always_at_tail(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-tail")

    e1 = UserBufferEntry.from_message({"role": "user", "content": "q1"}, pruned_at=time.time())
    e2 = UserBufferEntry.from_message({"role": "user", "content": "q2"}, pruned_at=time.time())

    await layer_set.user_retention.upsert_pruned_user(ctx, e1)
    await layer_set.user_retention.upsert_pruned_user(ctx, e2)

    # Both unfinished — both at tail
    entries = await layer_set.user_retention.get_entries(ctx)
    assert not entries[0].is_completed
    assert not entries[1].is_completed

    await layer_set.user_retention.mark_all_completed(ctx, "reply")

    # Now add a new user — should be unfinished at tail
    e3 = UserBufferEntry.from_message({"role": "user", "content": "q3"}, pruned_at=time.time())
    await layer_set.user_retention.upsert_pruned_user(ctx, e3)

    entries = await layer_set.user_retention.get_entries(ctx)
    assert entries[0].is_completed
    assert entries[1].is_completed
    assert not entries[2].is_completed  # tail is unfinished


@pytest.mark.asyncio
async def test_urb_clear(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-clear")

    e1 = UserBufferEntry.from_message({"role": "user", "content": "q1"}, pruned_at=time.time())
    await layer_set.user_retention.append_entries(ctx, [e1])

    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 1

    await layer_set.user_retention.clear(ctx)
    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_urb_replace_entries(registry: InMemoryStoreRegistry):
    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-replace")

    e1 = UserBufferEntry.from_message({"role": "user", "content": "q1"}, pruned_at=time.time())
    e2 = UserBufferEntry.from_message({"role": "user", "content": "q2"}, pruned_at=time.time())

    await layer_set.user_retention.append_entries(ctx, [e1])
    await layer_set.user_retention.replace_entries(ctx, [e2])

    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 1
    assert entries[0].pruned_user_content == "q2"


@pytest.mark.asyncio
async def test_urb_disabled_returns_none_from_factory():
    """When config.enabled=False, factory returns None for user_retention."""
    registry = InMemoryStoreRegistry()
    from framework.memory.layers.config import MemoryLayerConfigSet, UserRetentionBufferConfig

    config = MemoryLayerConfigSet(
        user_retention=UserRetentionBufferConfig(enabled=False),
    )
    layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)
    assert layer_set.user_retention is None


# ── XML format support ──


def test_entry_from_message_captures_content_format():
    from framework.memory.core.message import ContentFormat

    entry = UserBufferEntry.from_message(
        {
            "role": "user",
            "content": '<command_context><skill>x</skill></command_context>',
            "content_format": ContentFormat.XML,
            "truncatable_paths": ["command_context"],
        },
        pruned_at=time.time(),
    )
    assert entry.content_format == ContentFormat.XML
    assert entry.truncatable_paths == ["command_context"]


def test_entry_from_message_defaults_format_to_none():
    entry = UserBufferEntry.from_message(
        {"role": "user", "content": "plain text"},
        pruned_at=time.time(),
    )
    assert entry.content_format is None
    assert entry.truncatable_paths is None


def test_entry_roundtrip_preserves_xml_format():
    from framework.memory.core.message import ContentFormat

    entry = UserBufferEntry.from_message(
        {
            "role": "user",
            "content": "<root>data</root>",
            "content_format": ContentFormat.XML,
            "truncatable_paths": ["root"],
        },
        pruned_at=time.time(),
    )
    data = entry.to_dict()
    restored = UserBufferEntry.from_dict(data)
    assert restored is not None
    assert restored.content_format == ContentFormat.XML
    assert restored.truncatable_paths == ["root"]


@pytest.mark.asyncio
async def test_urb_xml_truncation_preserves_structure(registry: InMemoryStoreRegistry):
    """XML entries must be truncated using XML-safe method, not plain slicing."""
    from framework.memory.core.message import ContentFormat

    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-xml-trunc")

    # Build a long XML message that exceeds max_user_chars (4000)
    # user_input is truncatable; skill content is preserved.
    long_input = "y" * 5000
    xml_msg = (
        f'<command_context type="skill" name="test">\n'
        f"<skill>short skill</skill>\n"
        f"</command_context>\n\n"
        f"<user_input>\n{long_input}\n</user_input>"
    )
    entry = UserBufferEntry.from_message(
        {
            "role": "user",
            "content": xml_msg,
            "content_format": ContentFormat.XML,
            "truncatable_paths": ["command_context", "user_input"],
        },
        pruned_at=time.time(),
    )

    await layer_set.user_retention.upsert_pruned_user(ctx, entry)
    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 1

    truncated = entries[0].pruned_user_content
    # Must be shorter than original
    assert len(truncated) < len(xml_msg)
    # XML structure must be preserved — all opened tags must be closed
    assert truncated.count("<command_context") == truncated.count("</command_context>")
    assert truncated.count("<skill") == truncated.count("</skill>")
    assert truncated.count("<user_input") == truncated.count("</user_input>")
    # Must not have a raw cut in the middle of a tag
    assert not any(
        truncated.endswith(frag) for frag in ["<", "</", "<s", "<sk"]
    )


@pytest.mark.asyncio
async def test_urb_mark_all_completed_truncates_assistant_content(
    registry: InMemoryStoreRegistry,
):
    """completing_assistant_content MUST be truncated in mark_all_completed."""
    from framework.memory.layers.config import MemoryLayerConfigSet, UserRetentionBufferConfig

    # Create buffer with low max_assistant_chars to force truncation
    config = MemoryLayerConfigSet(
        user_retention=UserRetentionBufferConfig(
            max_entries=5, max_user_chars=4000, max_assistant_chars=100,
        ),
    )
    layer_set = MemoryLayerFactory.single_user(registry=registry, config=config)
    ctx = MemoryContext(session_id="test-trunc-assistant")

    entry = UserBufferEntry.from_message(
        {"role": "user", "content": "q"}, pruned_at=time.time(),
    )
    await layer_set.user_retention.upsert_pruned_user(ctx, entry)

    long_reply = "A" * 500  # well over max_assistant_chars=100
    await layer_set.user_retention.mark_all_completed(ctx, long_reply)

    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 1
    assert entries[0].is_completed
    assert len(entries[0].completing_assistant_content) <= 110, (
        f"Expected <= 110 chars, got {len(entries[0].completing_assistant_content)}"
    )


@pytest.mark.asyncio
async def test_urb_xml_truncation_fallback_when_paths_dont_cover_large_content(
    registry: InMemoryStoreRegistry,
):
    """When truncatable_paths don't include the actual large content area,
    XML-structured truncation may produce output that still exceeds max_chars.
    The enforcement must fall back to bounded truncation.

    Regression: content_format='xml' with truncatable_paths=['user_input'],
    but the 30KB content is inside <skill> which is NOT in the paths.
    truncate_xml_safe only truncated <user_input> (small) and left <skill>
    intact — result was still ~34KB, way over max_user_chars=4000.
    """
    from framework.memory.core.message import ContentFormat

    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-xml-fallback")

    # Real-world scenario: skill XML with <skill> containing ~30KB,
    # but truncatable_paths only lists 'user_input' — <skill> is missed.
    skill_body = "<step>" + ("long instruction text. " * 1000) + "</step>"
    xml_msg = (
        f'<command_context type="skill" name="huashu-design">\n'
        f"<skill>\n{skill_body}\n</skill>\n"
        f"</command_context>\n\n"
        f"<user_input>\nshort question here\n</user_input>"
    )
    assert len(xml_msg) > 5000, f"Test pre-condition failed: xml too short ({len(xml_msg)})"

    entry = UserBufferEntry.from_message(
        {
            "role": "user",
            "content": xml_msg,
            "content_format": ContentFormat.XML,
            # BUG: only 'user_input' listed — the real bulk is in <skill>
            "truncatable_paths": ["user_input"],
        },
        pruned_at=time.time(),
    )

    await layer_set.user_retention.upsert_pruned_user(ctx, entry)
    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 1

    truncated = entries[0].pruned_user_content
    # Phase 2 leaf-element safety net must have caught the large <step>
    # element that was not in truncatable_paths=['user_input'].
    # <step> is a leaf (no child elements) so it gets truncated to max_user_chars.
    assert len(truncated) < len(xml_msg), (
        f"Leaf-element safety net should have reduced content, "
        f"still {len(truncated)} chars (original {len(xml_msg)})"
    )
    # No single leaf element should exceed max_user_chars (4000)
    assert len(truncated) <= 5000, (
        f"Content should be around max_user_chars + XML overhead, "
        f"got {len(truncated)}"
    )


@pytest.mark.asyncio
async def test_urb_xml_truncation_empty_paths_still_bounded(
    registry: InMemoryStoreRegistry,
):
    """When truncatable_paths is empty on an XML entry with leaf elements,
    Phase 2 leaf-element safety net still catches the large leaves."""
    from framework.memory.core.message import ContentFormat

    layer_set = _make_layer_set(registry)
    ctx = MemoryContext(session_id="test-xml-nopath")

    huge_xml = "<root><data>" + ("A" * 8000) + "</data></root>"
    assert len(huge_xml) > 5000

    entry = UserBufferEntry.from_message(
        {
            "role": "user",
            "content": huge_xml,
            "content_format": ContentFormat.XML,
            "truncatable_paths": [],  # empty — Phase 1 returns early (boundary)
        },
        pruned_at=time.time(),
    )

    await layer_set.user_retention.upsert_pruned_user(ctx, entry)
    entries = await layer_set.user_retention.get_entries(ctx)
    assert len(entries) == 1

    truncated = entries[0].pruned_user_content
    assert len(truncated) <= 5000, (
        f"Empty paths: content should be bounded, got {len(truncated)}"
    )
