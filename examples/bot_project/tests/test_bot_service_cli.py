"""Tests for command-line parsing utilities."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modexbot.main import parse_args


def test_parse_args_has_no_mode_option() -> None:
    args = parse_args([])

    assert not hasattr(args, "mode")
