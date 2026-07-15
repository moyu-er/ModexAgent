"""User retention buffer — pruned user context with completion tracking."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole


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
    def from_message(
        cls, message: ChatMessage | dict[str, Any], *, pruned_at: float
    ) -> UserBufferEntry:
        """Create an entry from a pruned session message."""
        if isinstance(message, ChatMessage):
            # to_dict() omits content_format when it is the default (PLAIN),
            # preserving the "not explicitly set" semantics for UserBufferEntry.
            message_dict = message.to_dict()
        else:
            message_dict = dict(message)
        role = str(message_dict.get("role", "user"))
        if role not in {MessageRole.USER.value, MessageRole.AGENT.value}:
            raise ValueError(f"role must be user or agent, got {role}")
        content = cls._normalize_content(message_dict.get("content", ""))
        source_agent = message_dict.get("source_agent")
        source_text = str(source_agent) if source_agent is not None else None
        created_at = cls._coerce_timestamp(
            message_dict.get("created_at", message_dict.get("timestamp", pruned_at)),
            fallback=pruned_at,
        )
        content_format = message_dict.get("content_format")
        truncatable_paths = message_dict.get("truncatable_paths")

        # Auto-detect XML content if not explicitly tagged — preserves
        # truncatability for skill contexts and other XML-wrapped input.
        detected_format, detected_paths = cls._detect_xml_meta(
            content, content_format, truncatable_paths
        )

        return cls(
            pruned_user_role=role,
            pruned_user_content=content,
            pruned_user_source_agent=source_text,
            pruned_user_created_at=created_at,
            completing_assistant_content=None,
            fingerprint=cls._make_fingerprint(role, content, source_text),
            content_format=detected_format,
            truncatable_paths=detected_paths,
        )

    @staticmethod
    def _detect_xml_meta(
        content: str,
        content_format: str | None,
        truncatable_paths: list[str] | None,
    ) -> tuple[str | None, list[str] | None]:
        """Infer XML truncation metadata from explicit fields or content heuristic.

        If the message already carries content_format/truncatable_paths,
        those are preserved.  Otherwise, if the content looks like XML
        (starts with '<' and contains tags), format is inferred as 'xml'
        and a default path is provided so _enforce_limits can truncate
        safely without breaking structure.
        """
        if content_format is not None:
            paths: list[str] | None = None
            if truncatable_paths is not None and type(truncatable_paths) is list:
                paths = list(truncatable_paths)
            return str(content_format), paths
        stripped = content.strip()
        if stripped.startswith("<") and ">" in stripped:
            # Heuristic: content looks like XML — tag it so truncation
            # uses truncate_xml_safe instead of blunt head-cut.
            root_tag = stripped[1:].split()[0].split(">")[0].split("/")[0]
            return "xml", [root_tag] if root_tag else ["content"]
        return None, None

    @staticmethod
    def _make_fingerprint(role: str, content: str, source_agent: str | None) -> str:
        payload = {"role": role, "content": content, "source_agent": source_agent or ""}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_content(value: str | list[dict[str, Any]] | None) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return json.dumps(
                [dict(item) for item in value if isinstance(item, dict)], ensure_ascii=False
            )
        return str(value)

    @staticmethod
    def _coerce_timestamp(value: float | str | int | None, *, fallback: float) -> float:
        if value is None:
            return fallback
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            return UserBufferEntry._parse_timestamp_str(value, fallback)
        return fallback

    @staticmethod
    def _parse_timestamp_str(value: str, fallback: float) -> float:
        from datetime import datetime

        from modex_agent.utils.timezone import get_user_timezone

        tz = get_user_timezone()
        # Try session-style format first: "YYYY-MM-DD HH:MM:SS"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz)
                return dt.timestamp()
            except ValueError:
                pass
        # Try ISO format
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
        # Try plain float string
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Convert float timestamp to session-style readable format
        from datetime import datetime

        from modex_agent.utils.timezone import get_user_timezone

        dt = datetime.fromtimestamp(self.pruned_user_created_at, tz=get_user_timezone())
        d["pruned_user_created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        return d

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
            fp = str(
                data.get("fingerprint")
                or cls._make_fingerprint(role, content, str(source) if source else None)
            )
            content_format = data.get("content_format")
            truncatable_paths = data.get("truncatable_paths")
            return cls(
                pruned_user_role=role,
                pruned_user_content=content,
                pruned_user_source_agent=str(source) if source is not None else None,
                pruned_user_created_at=cls._coerce_timestamp(
                    data.get("pruned_user_created_at"), fallback=0.0
                ),
                completing_assistant_content=data.get("completing_assistant_content"),
                fingerprint=fp,
                content_format=str(content_format) if content_format is not None else None,
                truncatable_paths=list(truncatable_paths)
                if isinstance(truncatable_paths, list)
                else None,
            )
        except (TypeError, ValueError):
            return None

    @property
    def is_completed(self) -> bool:
        return self.completing_assistant_content is not None
