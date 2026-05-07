"""Tests for bot_service.py command-line parsing."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot_service import parse_args


def test_parse_args_defaults_to_pool_mode() -> None:
    args = parse_args([])

    assert args.mode == "pool"


def test_parse_args_accepts_pipeline_mode() -> None:
    args = parse_args(["--mode", "pipeline"])

    assert args.mode == "pipeline"
