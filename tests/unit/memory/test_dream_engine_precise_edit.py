"""Tests for DreamEngine Phase2 precise editing (replace_text mode)."""

import pytest

from framework.memory.core.consolidation import MemoryUpdate
from framework.memory.core.scope import MemoryContext, UserScope
from framework.memory.managers.long_term import LongTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    return s


@pytest.fixture
def context():
    return MemoryContext(session_id="sess_1")


@pytest.mark.asyncio
async def test_replace_text_mode_basic(storage, context):
    """replace_text 模式应精准替换匹配的文本。"""
    mgr = LongTermMemoryManager(storage, UserScope())
    await mgr.update(context, {"user": "- Name: Alice\n- Location: Tokyo\n"})

    update = MemoryUpdate(
        file_name="USER.md",
        content="- Location: Osaka\n",
        mode="replace_text",
        search_text="- Location: Tokyo\n",
    )
    result = await mgr.apply_update(context, update)

    assert "- Location: Osaka\n" in result
    assert "- Location: Tokyo\n" not in result
    assert "- Name: Alice" in result


@pytest.mark.asyncio
async def test_replace_text_fallback_when_not_found(storage, context):
    """search_text 未找到时应回退到 append。"""
    mgr = LongTermMemoryManager(storage, UserScope())
    await mgr.update(context, {"user": "- Name: Alice\n"})

    update = MemoryUpdate(
        file_name="USER.md",
        content="- Location: Osaka\n",
        mode="replace_text",
        search_text="- Location: Tokyo\n",  # not in existing
    )
    result = await mgr.apply_update(context, update)

    assert "- Name: Alice" in result
    assert "- Location: Osaka" in result


@pytest.mark.asyncio
async def test_replace_text_empty_search_text_fallback(storage, context):
    """search_text 为空时应回退到 append。"""
    mgr = LongTermMemoryManager(storage, UserScope())
    await mgr.update(context, {"user": "- Name: Alice\n"})

    update = MemoryUpdate(
        file_name="USER.md",
        content="- Location: Osaka\n",
        mode="replace_text",
        search_text="",
    )
    result = await mgr.apply_update(context, update)

    assert "- Name: Alice" in result
    assert "- Location: Osaka" in result


@pytest.mark.asyncio
async def test_replace_text_replaces_first_occurrence(storage, context):
    """replace_text 只替换第一个匹配项。"""
    mgr = LongTermMemoryManager(storage, UserScope())
    await mgr.update(context, {"user": "- Tag: alpha\n- Tag: beta\n"})

    update = MemoryUpdate(
        file_name="USER.md",
        content="- Tag: gamma\n",
        mode="replace_text",
        search_text="- Tag: alpha\n",
    )
    result = await mgr.apply_update(context, update)

    assert result.count("- Tag: gamma") == 1
    assert "- Tag: beta" in result


@pytest.mark.asyncio
async def test_append_mode_unchanged(storage, context):
    """append 模式保持原有行为。"""
    mgr = LongTermMemoryManager(storage, UserScope())
    await mgr.update(context, {"user": "- Name: Alice\n"})

    update = MemoryUpdate(
        file_name="USER.md",
        content="- Location: Osaka\n",
        mode="append",
    )
    result = await mgr.apply_update(context, update)

    assert "- Name: Alice" in result
    assert "- Location: Osaka" in result


@pytest.mark.asyncio
async def test_section_replace_mode_unchanged(storage, context):
    """section_replace 模式保持原有行为。"""
    mgr = LongTermMemoryManager(storage, UserScope())
    await mgr.update(context, {"user": "old content"})

    update = MemoryUpdate(
        file_name="USER.md",
        content="new content",
        mode="section_replace",
    )
    result = await mgr.apply_update(context, update)

    assert result == "new content"


def test_memory_update_has_search_text_field():
    """MemoryUpdate 应包含 search_text 字段。"""
    update = MemoryUpdate(
        file_name="USER.md",
        content="new",
        mode="replace_text",
        search_text="old",
    )
    assert update.search_text == "old"


def test_memory_update_search_text_defaults_to_empty():
    """MemoryUpdate search_text 默认值应为空字符串。"""
    update = MemoryUpdate(file_name="USER.md", content="new")
    assert update.search_text == ""
