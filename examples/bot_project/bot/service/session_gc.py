"""Crash-safe session garbage collection (ADR-0018).

A bot-side collector that deletes a conversation's full cascade (root + every
subagent descendant via ``parent_session_id``) and all ten per-session artifact
types. Crash recoverability is the first constraint: deletion progress is fully
reconstructable from disk (the session-index graph) after a restart, with no
in-memory closure collected up front. See ADR-0018 and the session-lifecycle
glossary in CONTEXT.md.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionGcConfig(BaseModel):
    """Global knobs for the session garbage collector (frozen, strict)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    scan_interval_seconds: int = Field(default=300, ge=1)
    max_workers: int = Field(default=1, ge=1)


def load_session_gc_config(raw: dict[str, Any] | None) -> SessionGcConfig:
    """Build SessionGcConfig from the raw top-level config dict.

    The framework ``AppConfig`` ignores business keys (``extra: ignore``), so the
    bot reads ``session_gc`` from the same raw YAML dict itself. Missing or empty
    → all defaults.
    """
    section = (raw or {}).get("session_gc") or {}
    return SessionGcConfig(**section)
