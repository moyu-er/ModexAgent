from __future__ import annotations

from typing import Any

import pytest

from framework.memory.core.layers import (
    ArchiveMemoryManager,
    KnowledgeMemoryManager,
    MemoryLayerSet,
    SessionMemoryManager,
)


def test_layer_set_uses_typed_fields_and_replacement_helpers() -> None:
    session = _SessionManager()
    archive = _ArchiveManager()
    knowledge = _KnowledgeManager()

    layers = MemoryLayerSet(session=session, archive=archive)
    replaced = layers.with_knowledge(knowledge)

    assert layers.session is session
    assert layers.archive is archive
    assert layers.knowledge is None
    assert replaced.session is session
    assert replaced.archive is archive
    assert replaced.knowledge is knowledge

    with pytest.raises(AttributeError):
        replaced.session = _SessionManager()  # type: ignore[misc]


class _SessionManager(SessionMemoryManager):
    async def add_messages(self, context: Any, messages: Any) -> Any:
        pass

    async def get_recent_messages(self, context: Any, limit: int | None = None) -> list[Any]:
        return []

    async def get_all_messages(self, context: Any) -> list[Any]:
        return []

    async def save_checkpoint(self, context: Any, messages: Any) -> None:
        pass

    async def load_checkpoint(self, context: Any) -> list[Any] | None:
        return None

    async def clear(self, context: Any) -> None:
        pass

    async def replace_messages(self, context: Any, messages: Any) -> Any:
        pass

    async def replace_messages_if_revision(
        self,
        context: Any,
        messages: Any,
        expected_revision: Any,
        state_updates: Any = None,
        idle_threshold_seconds: Any = None,
    ) -> Any:
        pass

    async def get_revision(self, context: Any) -> Any:
        pass

    async def get_checkpoint_id(self, context: Any) -> Any:
        return None

    async def clear_checkpoint(self, context: Any) -> None:
        pass


class _ArchiveManager(ArchiveMemoryManager):
    async def append(self, context: Any, entry: Any) -> Any:
        return entry

    async def get_recent(self, context: Any, limit: int = 5) -> list[Any]:
        return []

    async def search(self, context: Any, query: str, limit: int = 5) -> list[Any]:
        return []

    async def get_unprocessed(self, context: Any, cursor_name: str, limit: int = 100) -> Any:
        pass

    async def commit_cursor(self, context: Any, cursor_name: str, cursor: int) -> None:
        pass

    async def clear(self, context: Any) -> None:
        pass


class _KnowledgeManager(KnowledgeMemoryManager):
    async def get_all(self, context: Any) -> Any:
        pass

    async def get_file(self, context: Any, file_key: str) -> str | None:
        return None

    async def apply_update(self, context: Any, update: Any) -> str:
        return ""

    async def clear(self, context: Any) -> None:
        pass

    async def ensure_defaults(self, context: Any, defaults: Any = None) -> None:
        pass


class TestChatMessageReasoningContent:
    """ChatMessage 是纯粹的数据结构，思维链清理在业务层（ReActAgent）处理。

    from_dict() 保留原始字段（包括 reasoning_content 和 think 标签）；
    to_dict() 负责序列化安全，排除 reasoning_content。
    """

    def test_from_dict_preserves_reasoning_content(self):
        from framework.memory.core.message import ChatMessage

        msg = ChatMessage.from_dict({
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "thinking process",
        })
        # from_dict 保留 reasoning_content（清理在业务层做）
        assert msg.get("reasoning_content") == "thinking process"
        assert msg.content == "hello"

    def test_from_dict_preserves_think_tags_in_content(self):
        from framework.memory.core.message import ChatMessage

        msg = ChatMessage.from_dict({
            "role": "assistant",
            "content": "<think>reasoning</think>actual content",
        })
        # from_dict 保留 content 中的 think 标签（清理在业务层做）
        assert msg.content == "<think>reasoning</think>actual content"

    def test_to_dict_excludes_reasoning_content(self):
        from framework.memory.core.message import ChatMessage

        msg = ChatMessage(role="assistant", content="hello")
        # Simulate extra field via model_extra
        msg.__pydantic_extra__ = {"reasoning_content": "secret"}
        d = msg.to_dict()
        assert "reasoning_content" not in d
        assert d["content"] == "hello"

    def test_coerce_dict_preserves_raw_content(self):
        from framework.memory.core.message import ChatMessage

        msg = ChatMessage.coerce({
            "role": "assistant",
            "content": "<thinking>deep thought</thinking>result",
        })
        # coerce 保留原始 content（清理在业务层做）
        assert msg.content == "<thinking>deep thought</thinking>result"

    async def ensure_defaults(self, context: Any, defaults: Any = None) -> None:
        pass

    async def clear(self, context: Any) -> None:
        pass
