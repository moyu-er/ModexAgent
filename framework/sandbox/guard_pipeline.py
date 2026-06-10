"""Composite guard that runs multiple guards in sequence.

Usage::
    pipeline = GuardPipeline([
        CommandPatternGuard(),
        PathTraversalGuard(),
    ])
    result = pipeline.check("rm -rf /")
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .guard import GuardResult


@dataclass
class GuardPipeline:
    """Run a list of guards in order, short-circuiting on first denial.

    Each guard is a callable ``(str) -> GuardResult``.
    """
    guards: list[Callable[[str], GuardResult]] = field(default_factory=list)

    def check(self, command: str) -> GuardResult:
        """Run all guards in order.

        Returns the first denial result, or ``allowed=True`` if all pass.
        """
        all_matches: list = []
        for guard in self.guards:
            result = guard.check(command)
            if not result.allowed:
                all_matches.extend(result.matches)
                parts = [f"[{m.severity.value}] {m.description} ({m.category})" for m in all_matches]
                return GuardResult(
                    allowed=False,
                    matches=tuple(all_matches),
                    reason="Command denied: " + "; ".join(parts),
                )
        return GuardResult(allowed=True)
