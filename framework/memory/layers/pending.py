"""Pending pruned input memory manager."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.layers import PendingPrunedInputMemoryManager
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.config import PendingPrunedInputMemoryConfig, StorageFactory

_PENDING_MESSAGES_KEY = ".pending_pruned_inputs"


@dataclass(frozen=True)
class PendingPrunedInputEntry:
    """Stored unfinished user/agent input pruned from session memory."""

    role: MessageRole
    content: str | list[dict[str, Any]]
    source_agent: str | None
    created_at: float
    pruned_at: float
    fingerprint: str

    @classmethod
    def from_message(
        cls,
        message: dict[str, Any],
        *,
        pruned_at: float,
    ) -> PendingPrunedInputEntry:
        role = MessageRole(str(message.get("role", "")))
        if role not in {MessageRole.USER, MessageRole.AGENT}:
            raise ValueError(f"pending input role must be user or agent, got {role}")
        content = cls._normalize_content(message.get("content", ""))
        source_agent = message.get("source_agent")
        source_agent_text = str(source_agent) if source_agent is not None else None
        created_at = cls._coerce_timestamp(
            message.get("created_at", message.get("timestamp", pruned_at)),
            fallback=pruned_at,
        )
        return cls(
            role=role,
            content=content,
            source_agent=source_agent_text,
            created_at=created_at,
            pruned_at=pruned_at,
            fingerprint=cls.fingerprint_for(role, content, source_agent_text),
        )

    @staticmethod
    def fingerprint_for(
        role: MessageRole,
        content: str | list[dict[str, Any]],
        source_agent: str | None,
    ) -> str:
        payload = {
            "content": content,
            "role": role.value,
            "source_agent": source_agent or "",
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["role"] = self.role.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingPrunedInputEntry | None:
        try:
            role = MessageRole(str(data.get("role", "")))
            if role not in {MessageRole.USER, MessageRole.AGENT}:
                return None
            content = cls._normalize_content(data.get("content", ""))
            source_agent = data.get("source_agent")
            source_agent_text = str(source_agent) if source_agent is not None else None
            fingerprint = str(
                data.get("fingerprint")
                or cls.fingerprint_for(role, content, source_agent_text)
            )
            return cls(
                role=role,
                content=content,
                source_agent=source_agent_text,
                created_at=cls._coerce_timestamp(data.get("created_at"), fallback=0.0),
                pruned_at=cls._coerce_timestamp(data.get("pruned_at"), fallback=0.0),
                fingerprint=fingerprint,
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_content(value: Any) -> str | list[dict[str, Any]]:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        return "" if value is None else str(value)

    @staticmethod
    def _coerce_timestamp(value: Any, *, fallback: float) -> float:
        if isinstance(value, int | float):
            return float(value)
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return fallback


class ScopedPendingPrunedInputMemoryManager(PendingPrunedInputMemoryManager):
    """Pending layer manager backed by scoped memory storage."""

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: PendingPrunedInputMemoryConfig | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or PendingPrunedInputMemoryConfig()

    async def append_entries(
        self,
        context: MemoryContext,
        entries: Sequence[PendingPrunedInputEntry],
    ) -> None:
        if not self._config.enabled or not entries:
            return
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            existing = self._decode_entries(await storage.get(_PENDING_MESSAGES_KEY))
            ordered = existing
            for entry in entries:
                ordered = [
                    current for current in ordered
                    if current.fingerprint != entry.fingerprint
                ]
                ordered.append(entry)
            ordered = self._enforce_limits(ordered)
            await storage.set(_PENDING_MESSAGES_KEY, [entry.to_dict() for entry in ordered])

    async def get_entries(self, context: MemoryContext) -> list[PendingPrunedInputEntry]:
        if not self._config.enabled:
            return []
        storage = await self._storage_factory(context)
        return self._decode_entries(await storage.get(_PENDING_MESSAGES_KEY))

    async def replace_entries(
        self,
        context: MemoryContext,
        entries: Sequence[PendingPrunedInputEntry],
    ) -> None:
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            if not self._config.enabled:
                await storage.delete(_PENDING_MESSAGES_KEY)
                return
            ordered = self._enforce_limits(list(entries))
            await storage.set(_PENDING_MESSAGES_KEY, [entry.to_dict() for entry in ordered])

    async def clear(self, context: MemoryContext) -> None:
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            await storage.delete(_PENDING_MESSAGES_KEY)

    @staticmethod
    def _decode_entries(raw: Any) -> list[PendingPrunedInputEntry]:
        if not isinstance(raw, list):
            return []
        entries: list[PendingPrunedInputEntry] = []
        for item in raw:
            if isinstance(item, dict):
                entry = PendingPrunedInputEntry.from_dict(item)
                if entry is not None:
                    entries.append(entry)
        return entries

    def _enforce_limits(
        self,
        entries: list[PendingPrunedInputEntry],
    ) -> list[PendingPrunedInputEntry]:
        max_entries = max(0, self._config.max_entries)
        if max_entries == 0 or self._config.max_chars <= 0:
            return []
        kept = entries[-max_entries:]
        while kept and self._content_chars(kept) > self._config.max_chars:
            if len(kept) == 1:
                kept = [self._truncate_entry(kept[0], self._config.max_chars)]
                break
            kept = kept[1:]
        return kept

    @classmethod
    def _content_chars(cls, entries: Sequence[PendingPrunedInputEntry]) -> int:
        return sum(len(cls._content_to_text(entry.content)) for entry in entries)

    @staticmethod
    def _content_to_text(content: str | list[dict[str, Any]]) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _truncate_entry(
        cls,
        entry: PendingPrunedInputEntry,
        max_chars: int,
    ) -> PendingPrunedInputEntry:
        text = cls._content_to_text(entry.content)
        truncated_content = text[:max_chars]
        return replace(
            entry,
            content=truncated_content,
            fingerprint=PendingPrunedInputEntry.fingerprint_for(
                entry.role,
                truncated_content,
                entry.source_agent,
            ),
            pruned_at=time.time(),
        )
