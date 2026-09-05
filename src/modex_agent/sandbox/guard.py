"""Pattern-based command safety guard.

Uses regex deny/allow rules to block known-dangerous command patterns.
These are text checks, not containment of scripts or dynamic shell expansion.
SecurityDecisionService always installs the built-in deny rules; its per-tool
approval exemptions do not override guard findings. The legacy adapter config
below has separate command-pattern allowlist semantics.
For path traversal detection see ``guard_traversal.py``.
For SSRF detection see ``guard_network.py``.
For command-string path boundary checks see ``guard_path.py``.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple


class CommandSeverity(StrEnum):
    """Severity of a denied command match."""

    CRITICAL = "critical"  # Always blocked
    DANGEROUS = "dangerous"  # Blocked by default, can be overridden


@dataclass(frozen=True)
class GuardMatch:
    """Single match from command guard analysis."""

    pattern: str
    severity: CommandSeverity
    category: str  # "destructive", "privilege", "network_pipe", "fork_bomb", "system"
    description: str


@dataclass(frozen=True)
class GuardResult:
    """Result of a command guard check."""

    allowed: bool
    matches: tuple[GuardMatch, ...] = ()
    reason: str | None = None


class CommandGuard(ABC):
    @abstractmethod
    def check(self, command: str) -> GuardResult:
        ...


@dataclass
class CommandPatternGuardConfig:
    """Configuration for CommandPatternGuard."""

    extra_deny_patterns: list[str] = field(default_factory=list)
    allow_patterns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal rule representation
# ---------------------------------------------------------------------------


class _DenyRule(NamedTuple):
    """Compiled deny rule used internally by CommandPatternGuard."""

    pattern: re.Pattern[str]
    severity: CommandSeverity
    category: str
    description: str


# ---------------------------------------------------------------------------
# Default deny rule definitions
# ---------------------------------------------------------------------------

_DEFAULT_DENY_RULES: list[tuple[str, CommandSeverity, str, str]] = [
    # -- CRITICAL (always blocked) --
    (
        r"\brm\s+-rf\s+/\b",
        CommandSeverity.CRITICAL,
        "destructive",
        "Recursive force delete root",
    ),
    (
        r"\brm\s+-[rf]{1,2}\b",
        CommandSeverity.CRITICAL,
        "destructive",
        "Recursive force delete",
    ),
    (
        r"\bdel\s+/[fq]\b",
        CommandSeverity.CRITICAL,
        "destructive",
        "Windows force delete",
    ),
    (
        r"\brmdir\s+/s\b",
        CommandSeverity.CRITICAL,
        "destructive",
        "Windows recursive directory removal",
    ),
    (
        r"(?:^|[;&|]\s*)format(?!=)\b",
        CommandSeverity.CRITICAL,
        "destructive",
        "Disk format",
    ),
    (
        r"\bmkfs\b",
        CommandSeverity.CRITICAL,
        "destructive",
        "Filesystem format",
    ),
    (
        r"\bdd\s+.*of=/dev/",
        CommandSeverity.CRITICAL,
        "destructive",
        "Direct disk write",
    ),
    (
        r":\(\)\s*\{",
        CommandSeverity.CRITICAL,
        "fork_bomb",
        "Fork bomb pattern",
    ),
    (
        r"\b(shutdown|reboot|poweroff)\b",
        CommandSeverity.CRITICAL,
        "system",
        "System power control",
    ),
    # -- DANGEROUS (blocked by default, exempted via allow_patterns) --
    (
        r"\bsudo\b",
        CommandSeverity.DANGEROUS,
        "privilege",
        "Privilege escalation via sudo",
    ),
    (
        r"\bsu\s+-",
        CommandSeverity.DANGEROUS,
        "privilege",
        "Switch user",
    ),
    (
        r"curl\b.*\|\s*(ba)?sh",
        CommandSeverity.DANGEROUS,
        "network_pipe",
        "Piping curl to shell",
    ),
    (
        r"wget\b.*\|\s*(ba)?sh",
        CommandSeverity.DANGEROUS,
        "network_pipe",
        "Piping wget to shell",
    ),
]


class CommandPatternGuard(CommandGuard):
    """Regex-based deny/allow command guard.

    ONLY does pattern matching. For path/network checks, compose with
    other guards via GuardPipeline.
    """

    def __init__(self, config: CommandPatternGuardConfig | None = None) -> None:
        self._config = config or CommandPatternGuardConfig()
        self._deny_rules = self._build_deny_rules()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, command: str) -> GuardResult:
        """Analyse *command* and return a :class:`GuardResult`.

        Evaluation order:

        1. **deny_patterns evaluation** — every deny rule is tested.
        2. **CRITICAL findings deny unconditionally** — no allow pattern
           can exempt them.
        3. **allow_patterns exemption** — an allow hit exempts the
           remaining (DANGEROUS-tier) findings.
        4. **allowlist enforcement** — when ``allow_patterns`` is
           configured but the command did *not* match any, the command
           is blocked (whitelist mode).
        5. Otherwise the command is allowed.
        """
        lowered = command.lower()
        matches: list[GuardMatch] = []

        # Step 1 -- deny-rule evaluation.
        for rule in self._deny_rules:
            if rule.pattern.search(lowered):
                matches.append(
                    GuardMatch(
                        pattern=rule.pattern.pattern,
                        severity=rule.severity,
                        category=rule.category,
                        description=rule.description,
                    )
                )

        # Step 2 -- CRITICAL findings survive any allow-list hit.
        if any(m.severity is CommandSeverity.CRITICAL for m in matches):
            return self._denied(matches)

        # Step 3 -- allow hit exempts the remaining findings.
        if self._config.allow_patterns and any(
            re.search(allow_pat, lowered) for allow_pat in self._config.allow_patterns
        ):
            return GuardResult(allowed=True)

        # Step 4 -- allowlist enforcement (whitelist mode).
        # If allow_patterns was configured and we reached here, the
        # command did NOT match any allow pattern -> block it.
        if self._config.allow_patterns:
            matches.append(
                GuardMatch(
                    pattern="<allowlist>",
                    severity=CommandSeverity.CRITICAL,
                    category="allowlist",
                    description="Not in allowlist",
                )
            )

        # Step 5 -- remaining deny findings block; clean commands pass.
        if matches:
            return self._denied(matches)

        return GuardResult(allowed=True)

    @staticmethod
    def _denied(matches: list[GuardMatch]) -> GuardResult:
        parts = [f"[{m.severity.value}] {m.description} ({m.category})" for m in matches]
        return GuardResult(
            allowed=False,
            matches=tuple(matches),
            reason="Command denied: " + "; ".join(parts),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_deny_rules(self) -> list[_DenyRule]:
        """Compile the default deny rules plus any extra patterns from config."""
        rules: list[_DenyRule] = []

        for raw, severity, category, desc in _DEFAULT_DENY_RULES:
            rules.append(
                _DenyRule(
                    pattern=re.compile(raw, re.IGNORECASE),
                    severity=severity,
                    category=category,
                    description=desc,
                )
            )

        # Extra deny patterns from config are treated as CRITICAL.
        for raw in self._config.extra_deny_patterns:
            rules.append(
                _DenyRule(
                    pattern=re.compile(raw, re.IGNORECASE),
                    severity=CommandSeverity.CRITICAL,
                    category="custom",
                    description=f"Custom deny rule: {raw}",
                )
            )

        return rules
