"""Mutable Pydantic state shared by nodes in a graph execution."""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class GraphState(BaseModel):
    """Mutable Pydantic state with snapshot serialization."""

    model_config = ConfigDict(
        # Mutable: imperative `ctx.state.x = y` is allowed (D4 Z-style).
        # Individual value-object fields may be frozen Pydantic models.
        frozen=False,
        # Strict: extra fields are errors. Subclasses declare all state.
        extra="forbid",
        # Validate on assignment so imperative mutations run validators.
        validate_assignment=False,
        # Allow arbitrary types for non-Pydantic runtime values.
        arbitrary_types_allowed=True,
    )

    # Resume target set by ``ctx.interrupt(value, resume_to=...)``; the
    # entry node routes via ``deliver(content, target, ctx)`` on re-entry,
    # then clears it. Replaces entry-node phase hardcoding.
    resume_target: str | None = None

    # Per-node working state. Each node writes only to
    # ``node_scratch[self.node_id]``; key separation provides isolation.
    node_scratch: dict[str, Any] = Field(default_factory=dict)

    def checkpoint(self) -> dict[str, JsonValue]:
        """Serialize state to a JSON-compatible dictionary."""
        return self.model_dump(mode="json")

    @classmethod
    def from_checkpoint(cls, data: dict[str, JsonValue]) -> Self:
        """Reconstruct a state instance from a checkpoint dictionary."""
        return cls.model_validate(data)


__all__ = ["GraphState"]
