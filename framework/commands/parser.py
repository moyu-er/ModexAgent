from __future__ import annotations

import re

from framework.commands.constants import CommandParseStatus
from framework.commands.models import CommandParseResult, SlashCommandInvocation

_COMMAND_RE = re.compile(r"^/([a-z0-9_-]+)(?: +(.*))?$")


class SlashCommandParser:
    """Pure parser for slash-command syntax."""

    def parse(self, text: str) -> CommandParseResult:
        if not text.startswith("/"):
            return CommandParseResult(status=CommandParseStatus.PLAIN_INPUT)

        match = _COMMAND_RE.fullmatch(text)
        if match is None:
            return CommandParseResult(
                status=CommandParseStatus.INVALID_COMMAND,
                error="invalid slash-command syntax",
            )

        command = match.group(1)
        args = match.group(2) or ""
        return CommandParseResult(
            status=CommandParseStatus.VALID_COMMAND,
            invocation=SlashCommandInvocation(command=command, args=args, raw=text),
        )
