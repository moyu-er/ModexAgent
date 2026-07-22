"""Injection XML element tag names — shared between injection, governance, and truncation.

Each StrEnum ensures injection code, truncation code, and tests use the same
literal strings.  Values are the XML element tag names exposed to the LLM.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CoreMemoryTag",
    "ArchiveTag",
    "PrunedTag",
    "UrbTag",
]


class CoreMemoryTag(StrEnum):
    """XML element tag names for injected persistent core memory."""

    YOUR_IDENTITY = "your_identity"
    USER_PROFILE = "user_profile"
    KNOWN_FACTS = "known_facts"


class ArchiveTag(StrEnum):
    """XML element tag names for injected archive summaries."""

    CONTAINER = "older_topics"
    SUMMARY = "summary"


class PrunedTag(StrEnum):
    """XML element tag names for injected pruned transcript catalog."""

    CONTAINER = "full_transcripts"
    HISTORY = "history"
    TRANSCRIPT = "transcript"


class UrbTag(StrEnum):
    """XML element tag names for injected user retention buffer fragments."""

    CONTAINER = "recent_messages"
    ENTRY = "entry"
    USER_MSG = "user"
    YOU_RESPONSE = "you"
