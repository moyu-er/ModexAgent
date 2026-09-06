"""Composite guard that runs multiple guards in sequence.

Usage::
    pipeline = GuardPipeline([
        CommandPatternGuard(),
    ])
    result = pipeline.check("rm -rf /")
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .guard import CommandGuard, GuardMatch, GuardResult


class GuardPipeline(BaseModel):
    """Run a list of guards in order, short-circuiting on first denial.

    Each guard implements :class:`CommandGuard`. ``check`` traverses the
    configured list without changing it; guards may resolve filesystem paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    guards: list[CommandGuard] = Field(default_factory=list)

    def __init__(self, guards: list[CommandGuard] | None = None) -> None:
        super().__init__(guards=guards if guards is not None else [])

    def check(self, command: str) -> GuardResult:
        """Run all guards in order.

        Returns the first denial result, or ``allowed=True`` if all pass.
        """
        all_matches: list[GuardMatch] = []
        for guard in self.guards:
            result = guard.check(command)
            if not result.allowed:
                all_matches.extend(result.matches)
                parts = [
                    f"[{m.severity.value}] {m.description} ({m.category})" for m in all_matches
                ]
                return GuardResult(
                    allowed=False,
                    matches=tuple(all_matches),
                    reason="Command denied: " + "; ".join(parts),
                )
        return GuardResult(allowed=True)
