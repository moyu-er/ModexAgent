from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PrunedIndexEntry:
    """Index entry for a single pruned batch stored on disk.

    Each entry is persisted as one JSON line in ``index.jsonl``.
    The ``*_display`` fields are human-readable and intended for LLM
    consumption.  The plain ``*_time`` fields hold epoch seconds for
    internal sorting and eviction.
    """

    # -- required fields --
    id: int
    cleanup_time: int           # epoch seconds (internal)
    cleanup_time_display: str   # "YYYY-MM-DD HH:MM" (display)
    message_count: int
    content_filename: str       # exactly matches the actual file name on disk

    # -- optional fields --
    start_time: int = 0              # epoch seconds (internal)
    end_time: int = 0                # epoch seconds (internal)
    start_time_display: str = ""     # "YYYY-MM-DD HH:MM" (display)
    end_time_display: str = ""       # "YYYY-MM-DD HH:MM" (display)
    topic: str = ""                  # from archive CONTEXT summary, or time-range fallback

    def to_dict(self) -> dict:
        """Serialize all fields to a plain dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PrunedIndexEntry:
        """Deserialize from a dict, ignoring unknown keys.

        Missing optional fields fall back to their defaults.
        """
        known = {
            "id",
            "cleanup_time", "cleanup_time_display",
            "message_count",
            "content_filename",
            "start_time", "end_time",
            "start_time_display", "end_time_display",
            "topic",
        }
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
