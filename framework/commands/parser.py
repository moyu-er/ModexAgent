from __future__ import annotations

import re

from framework.commands.constants import CommandParseStatus
from framework.commands.models import CommandParseResult, SlashCommandInvocation

_COMMAND_RE = re.compile(r"^/([a-z0-9_-]+)(?: +(.*))?$")


def _sanitize_input(text: str) -> str:
    """Strip invisible Unicode cruft that QQ/Discord/etc. may inject.

    Handles zero-width spaces (U+200B), BOM (U+FEFF), and other invisible
    characters that ``str.strip()`` does not remove, so that a message like
    ``\\u200b/huashu-design`` is still recognized as a slash command.
    """
    stripped = text.lstrip(
        "​‌‍‎‏"   # zero-width / directional
        "﻿"                            # BOM
        "  ᠎  　"  # Unicode spaces strip() may miss
    )
    return stripped


class SlashCommandParser:
    """Pure parser for slash-command syntax."""

    def parse(self, text: str) -> CommandParseResult:
        stripped = text.strip()

        # First pass: if text starts with / after standard strip(), fast path
        if stripped.startswith("/"):
            match = _COMMAND_RE.fullmatch(stripped)
            if match is not None:
                command = match.group(1)
                args = match.group(2) or ""
                return CommandParseResult(
                    status=CommandParseStatus.VALID_COMMAND,
                    invocation=SlashCommandInvocation(command=command, args=args, raw=text),
                )
            return CommandParseResult(
                status=CommandParseStatus.INVALID_COMMAND,
                error="invalid slash-command syntax",
            )

        # Second pass: try stripping invisible Unicode cruft
        sanitized = _sanitize_input(text)
        if sanitized is not text and sanitized.startswith("/"):
            match = _COMMAND_RE.fullmatch(sanitized)
            if match is not None:
                command = match.group(1)
                args = match.group(2) or ""
                return CommandParseResult(
                    status=CommandParseStatus.VALID_COMMAND,
                    invocation=SlashCommandInvocation(command=command, args=args, raw=text),
                )
            return CommandParseResult(
                status=CommandParseStatus.INVALID_COMMAND,
                error="invalid slash-command syntax",
            )

        return CommandParseResult(status=CommandParseStatus.PLAIN_INPUT)
