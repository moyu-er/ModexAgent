"""Path traversal detection guard.

Scans command strings for ``../`` or ``..\\`` path traversal sequences.
"""

from __future__ import annotations

from dataclasses import dataclass

from .guard import CommandGuard, CommandSeverity, GuardMatch, GuardResult


@dataclass
class PathTraversalConfig:
    enabled: bool = True


class PathTraversalGuard(CommandGuard):
    """Detect path traversal sequences in command strings."""

    def __init__(self, config: PathTraversalConfig | None = None) -> None:
        self._config = config or PathTraversalConfig()

    def check(self, command: str) -> GuardResult:
        if not self._config.enabled:
            return GuardResult(allowed=True)

        if "../" in command or "..\\" in command:
            match = GuardMatch(
                pattern="<path-traversal>",
                severity=CommandSeverity.CRITICAL,
                category="path_traversal",
                description="Path traversal detected",
            )
            return GuardResult(
                allowed=False,
                matches=(match,),
                reason="Command denied: [critical] Path traversal detected (path_traversal)",
            )

        return GuardResult(allowed=True)
