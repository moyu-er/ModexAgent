from __future__ import annotations

import pytest

from framework.commands.constants import CommandParseStatus
from framework.commands.parser import SlashCommandParser


@pytest.mark.parametrize(
    "text",
    ["hello", "hello /continue", "", "／huashu-design"],
)
def test_plain_input_when_slash_is_not_first_character(text: str) -> None:
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.PLAIN_INPUT
    assert result.invocation is None


@pytest.mark.parametrize(
    ("text", "command", "args"),
    [
        ("/continue", "continue", ""),
        ("/skill-name run this", "skill-name", "run this"),
        ("/skill_name    run   this", "skill_name", "run   this"),
        ("/a1-b2_c3", "a1-b2_c3", ""),
        # strip() makes leading space forgiving
        (" /continue", "continue", ""),
        # trailing newline stripped by strip()
        ("/continue\n", "continue", ""),
    ],
)
def test_valid_commands(text: str, command: str, args: str) -> None:
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.VALID_COMMAND
    assert result.invocation is not None
    assert result.invocation.command == command
    assert result.invocation.args == args
    assert result.invocation.raw == text


@pytest.mark.parametrize(
    "text",
    ["/", "/Command", "/cmd.foo", "/cmd:foo", "/cmd\targ", "/cmd/arg"],
)
def test_invalid_command_syntax(text: str) -> None:
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.INVALID_COMMAND
    assert result.invocation is None
    assert result.error is not None


@pytest.mark.parametrize(
    ("text", "expected_cmd"),
    [
        # zero-width space before slash — sanitizer strips it
        ("​/huashu-design", "huashu-design"),
        # BOM before slash — sanitizer strips it
        ("﻿/huashu-design", "huashu-design"),
    ],
)
def test_sanitizer_handles_invisible_unicode(text: str, expected_cmd: str) -> None:
    """Zero-width chars and BOM before slash are stripped so the command is recognized."""
    result = SlashCommandParser().parse(text)
    assert result.status == CommandParseStatus.VALID_COMMAND
    assert result.invocation is not None
    assert result.invocation.command == expected_cmd
    assert result.invocation.raw == text
