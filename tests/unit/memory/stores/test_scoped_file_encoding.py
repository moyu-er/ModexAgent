"""Tests for DefaultScopedStorage encoding fallback resilience."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modex_agent.memory.core.scope import MemoryLayerName
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage


@pytest.mark.asyncio
async def test_load_messages_utf8_normal(tmp_path: Path) -> None:
    """UTF-8 Chinese messages load correctly (baseline)."""
    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    await storage.initialize()

    # Write valid UTF-8 messages with Chinese
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
    ]
    for msg in messages:
        await storage.append_message(msg)

    loaded = await storage.load_messages()
    assert len(loaded) == 2
    assert loaded[0]["content"] == "你好"
    assert loaded[1]["content"] == "你好！有什么可以帮助你的？"


@pytest.mark.asyncio
async def test_load_messages_gbk_fallback(tmp_path: Path) -> None:
    """When messages.jsonl contains GBK-encoded data, auto-recover via fallback."""
    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    await storage.initialize()

    # Simulate: write messages.jsonl with GBK encoding directly (bypass append_message)
    messages = [
        {"role": "user", "content": "你是谁"},
        {"role": "assistant", "content": "我是AI助手"},
    ]
    msg_path = tmp_path / "messages.jsonl"
    with msg_path.open("w", encoding="gbk") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # Verify the file is NOT valid UTF-8
    with msg_path.open("rb") as f:
        raw = f.read()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")

    # load_messages should recover via GBK fallback
    loaded = await storage.load_messages()
    assert len(loaded) == 2
    assert loaded[0]["content"] == "你是谁"
    assert loaded[1]["content"] == "我是AI助手"


@pytest.mark.asyncio
async def test_load_messages_corrupted_backup_and_start_fresh(tmp_path: Path) -> None:
    """Completely unreadable file → backup + return empty list, log error."""
    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    await storage.initialize()

    # Write binary garbage (not valid in any text encoding)
    msg_path = tmp_path / "messages.jsonl"
    msg_path.write_bytes(b"\x80\x81\x82\x83\xff\xfe\xfd\xfc")

    # load_messages should NOT crash; it should backup and return []
    loaded = await storage.load_messages()
    assert loaded == []

    # Backup file should exist
    backup_path = tmp_path / "messages.jsonl.bak"
    assert backup_path.exists()
    assert backup_path.read_bytes() == b"\x80\x81\x82\x83\xff\xfe\xfd\xfc"

    # Original file should be gone or empty
    assert not msg_path.exists() or msg_path.stat().st_size == 0


@pytest.mark.asyncio
async def test_load_messages_empty_file(tmp_path: Path) -> None:
    """Empty messages.jsonl returns empty list."""
    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    await storage.initialize()

    # Create empty file
    (tmp_path / "messages.jsonl").write_text("", encoding="utf-8")

    loaded = await storage.load_messages()
    assert loaded == []


@pytest.mark.asyncio
async def test_load_messages_missing_file(tmp_path: Path) -> None:
    """Missing messages.jsonl returns empty list."""
    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    await storage.initialize()

    loaded = await storage.load_messages()
    assert loaded == []


@pytest.mark.asyncio
async def test_load_messages_mixed_valid_and_invalid_lines(tmp_path: Path) -> None:
    """Lines that fail JSON parse are skipped, valid lines are kept."""
    storage = DefaultScopedStorage(tmp_path, layer=MemoryLayerName.SESSION)
    await storage.initialize()

    msg_path = tmp_path / "messages.jsonl"
    # Write a mix: valid JSON, then garbage line, then valid JSON
    with msg_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "hello"}, ensure_ascii=False) + "\n")
        f.write("this is not valid json\n")
        f.write(json.dumps({"role": "assistant", "content": "world"}, ensure_ascii=False) + "\n")

    loaded = await storage.load_messages()
    assert len(loaded) == 2
    assert loaded[0]["content"] == "hello"
    assert loaded[1]["content"] == "world"
