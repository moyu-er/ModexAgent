"""Shared fixtures for sandbox unit tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest

from modex_agent.sandbox import engine_probe


@pytest.fixture(autouse=True)
def _clean_probe_cache() -> Generator[None]:
    """Probe results are cached per process — isolate tests from each other."""
    engine_probe.clear_probe_cache()
    yield
    engine_probe.clear_probe_cache()
