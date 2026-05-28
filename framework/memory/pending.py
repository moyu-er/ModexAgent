"""Pending-pruned-input extraction and injection helpers."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.layers import PendingPrunedInputMemoryManager, SessionMemoryManager
from framework.memory.core.message import ContentFormat
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.pending import PendingPrunedInputEntry

logger = logging.getLogger(__name__)


def _xml_escape(text: str) -> str:
    """Escape special XML characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class PendingPrunedInputExtractor(ABC):
    """Extract unfinished user/agent inputs from a compression prune set."""

    @abstractmethod
    def extract(
        self,
        messages: Sequence[dict[str, Any]],
        pruned_indices: set[int],
    ) -> list[PendingPrunedInputEntry]:
        pass


class DefaultPendingPrunedInputExtractor(PendingPrunedInputExtractor):
    """Default extractor for pruned but not yet completed user/agent inputs."""

    def extract(
        self,
        messages: Sequence[dict[str, Any]],
        pruned_indices: set[int],
    ) -> list[PendingPrunedInputEntry]:
        open_inputs: list[PendingPrunedInputEntry] = []
        pruned_at = time.time()
        for index, message in enumerate(messages):
            role = message.get("role")
            if role in {MessageRole.USER.value, MessageRole.AGENT.value}:
                if index in pruned_indices:
                    try:
                        open_inputs.append(
                            PendingPrunedInputEntry.from_message(
                                message,
                                pruned_at=pruned_at,
                            )
                        )
                    except ValueError:
                        logger.debug("Skipping invalid pending input message", exc_info=True)
                continue
            if role == MessageRole.ASSISTANT.value and not message.get("tool_calls"):
                open_inputs.clear()
        return open_inputs


class PendingPrunedInputInjector(ABC):
    """Inject pending entries into provider-visible messages."""

    @abstractmethod
    async def apply(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
    ) -> list[dict[str, Any]]:
        pass


class DefaultPendingPrunedInputInjector(PendingPrunedInputInjector):
    """Default injector that emits one provider-compatible user message."""

    def __init__(
        self,
        manager: PendingPrunedInputMemoryManager,
        session: SessionMemoryManager | None = None,
    ) -> None:
        self._manager = manager
        self._session = session

    async def apply(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
    ) -> list[dict[str, Any]]:
        if await self._clear_if_session_completed(context):
            return messages
        try:
            entries = await self._manager.get_entries(context)
        except Exception:
            logger.warning("Failed to load pending pruned inputs", exc_info=True)
            return messages
        if not entries:
            return messages

        # Build XML content
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        xml_parts = [
            f'<supplementary-context type="pending-input" entries="{len(entries)}" timestamp="{ts}">'
        ]
        for entry in entries:
            source = "user"
            content = self._entry_content(entry)
            xml_parts.append(f'  <entry source="{_xml_escape(source)}">')
            xml_parts.append(f'    <content>{_xml_escape(content)}</content>')
            xml_parts.append(f'  </entry>')
        xml_parts.append('</supplementary-context>')
        xml_content = "\n".join(xml_parts)

        pending_message = {
            "role": MessageRole.SYSTEM.value,
            "content": xml_content,
            "content_format": ContentFormat.XML,
            "truncatable_paths": ["content"],
            "metadata": {
                "memory_source": "pending_pruned_inputs",
                "entry_count": len(entries),
            },
        }
        insert_at = self._after_system_messages(messages)
        return [*messages[:insert_at], pending_message, *messages[insert_at:]]

    async def _clear_if_session_completed(self, context: MemoryContext) -> bool:
        if self._session is None:
            return False
        try:
            session_messages = await self._session.get_all_messages(context)
        except Exception:
            logger.debug("Unable to inspect session before pending injection", exc_info=True)
            return False
        for message in session_messages:
            data = message.to_dict() if hasattr(message, "to_dict") else dict(message)
            if data.get("role") == MessageRole.ASSISTANT.value and not data.get("tool_calls"):
                try:
                    await self._manager.clear(context)
                except Exception:
                    logger.warning("Failed to clear completed pending inputs", exc_info=True)
                return True
        return False

    @staticmethod
    def _entry_content(entry: PendingPrunedInputEntry) -> str:
        if isinstance(entry.content, str):
            return entry.content
        return json.dumps(entry.content, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _after_system_messages(messages: Sequence[dict[str, Any]]) -> int:
        index = 0
        while index < len(messages) and messages[index].get("role") == MessageRole.SYSTEM.value:
            index += 1
        return index
