"""User retention buffer — pruned user context with completion tracking."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.scope import MemoryContext


@dataclass(frozen=True)
class UserBufferEntry:
    """A pruned user message tracked in the retention buffer.

    Two states based on ``completing_assistant_content``:
    - None = unfinished (no plain assistant has responded yet)
    - str  = completed (holds the assistant's response content)

    ``content_format`` and ``truncatable_paths`` carry the original
    message's format metadata so XML-safe truncation can be applied
    when the entry content exceeds configured limits.
    """

    pruned_user_role: str
    pruned_user_content: str
    pruned_user_source_agent: str | None
    pruned_user_created_at: float
    completing_assistant_content: str | None
    fingerprint: str
    content_format: str | None = None
    truncatable_paths: list[str] | None = None

    @classmethod
    def from_message(cls, message: dict[str, Any], *, pruned_at: float) -> UserBufferEntry:
        """Create an entry from a pruned session message dict."""
        role = str(message.get("role", "user"))
        if role not in {MessageRole.USER.value, MessageRole.AGENT.value}:
            raise ValueError(f"role must be user or agent, got {role}")
        content = cls._normalize_content(message.get("content", ""))
        source_agent = message.get("source_agent")
        source_text = str(source_agent) if source_agent is not None else None
        created_at = cls._coerce_timestamp(
            message.get("created_at", message.get("timestamp", pruned_at)),
            fallback=pruned_at,
        )
        content_format = message.get("content_format")
        truncatable_paths = message.get("truncatable_paths")
        return cls(
            pruned_user_role=role,
            pruned_user_content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            pruned_user_source_agent=source_text,
            pruned_user_created_at=created_at,
            completing_assistant_content=None,
            fingerprint=cls._make_fingerprint(role, content, source_text),
            content_format=str(content_format) if content_format is not None else None,
            truncatable_paths=list(truncatable_paths) if isinstance(truncatable_paths, list) else None,
        )

    @staticmethod
    def _make_fingerprint(role: str, content: str, source_agent: str | None) -> str:
        payload = {"role": role, "content": content, "source_agent": source_agent or ""}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_content(value: Any) -> str:
        if isinstance(value, list):
            return json.dumps([dict(item) for item in value if isinstance(item, dict)], ensure_ascii=False)
        return "" if value is None else str(value)

    @staticmethod
    def _coerce_timestamp(value: Any, *, fallback: float) -> float:
        if isinstance(value, int | float):
            return float(value)
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return fallback

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserBufferEntry | None:
        try:
            role = str(data.get("pruned_user_role", ""))
            if role not in {MessageRole.USER.value, MessageRole.AGENT.value}:
                return None
            content = data.get("pruned_user_content", "")
            if not isinstance(content, str):
                content = str(content)
            source = data.get("pruned_user_source_agent")
            fp = str(data.get("fingerprint")
                or cls._make_fingerprint(role, content, str(source) if source else None))
            content_format = data.get("content_format")
            truncatable_paths = data.get("truncatable_paths")
            return cls(
                pruned_user_role=role,
                pruned_user_content=content,
                pruned_user_source_agent=str(source) if source is not None else None,
                pruned_user_created_at=cls._coerce_timestamp(data.get("pruned_user_created_at"), fallback=0.0),
                completing_assistant_content=data.get("completing_assistant_content"),
                fingerprint=fp,
                content_format=str(content_format) if content_format is not None else None,
                truncatable_paths=list(truncatable_paths) if isinstance(truncatable_paths, list) else None,
            )
        except (TypeError, ValueError):
            return None

    @property
    def is_completed(self) -> bool:
        return self.completing_assistant_content is not None
