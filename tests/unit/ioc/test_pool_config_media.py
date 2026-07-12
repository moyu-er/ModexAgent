"""Tests for MediaConfig defaults (ADR-0013 §7)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from modex_agent.multi_agent.pool_config.media import MediaConfig

_MB = 1024 * 1024
_GB = 1024 * _MB


class TestMediaConfigDefaults:
    def test_image_cap_20mb(self) -> None:
        assert MediaConfig().max_image_bytes == 20 * _MB

    def test_text_doc_cap_10mb(self) -> None:
        assert MediaConfig().max_text_doc_bytes == 10 * _MB

    def test_session_budget_500mb(self) -> None:
        assert MediaConfig().session_budget_bytes == 500 * _MB

    def test_outbound_cap_1gb(self) -> None:
        assert MediaConfig().max_outbound_bytes == _GB

    def test_frozen(self) -> None:
        cfg = MediaConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.max_image_bytes = 1  # type: ignore[misc]
