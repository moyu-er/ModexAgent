"""Agent communication/session kind — topology only, not memory policy."""

from __future__ import annotations

from enum import StrEnum


class AgentCommKind(StrEnum):
    NORMAL = "normal"
    SUBAGENT = "subagent"
