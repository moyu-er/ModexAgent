"""Shared fixtures for multi_agent unit tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_modexctl_bin_dir():
    """Auto-mock ``resolve_modexctl_bin_dir`` so template.materialize works
    without ``modexctl`` installed.

    ``template.py`` imports the function directly, so the patch must target
    the local reference in ``template``, not the source module.
    """
    with patch(
        "modex_agent.agents.external.cli_resolver.resolve_modexctl_bin_dir",
        return_value=Path("/fake/bin"),
    ):
        yield
