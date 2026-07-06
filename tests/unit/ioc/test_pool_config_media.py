"""Tests for MediaConfig + PoolConfig.media wiring (ADR-0013 §7)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.pool import MediaConfig, PoolConfig

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


class TestPoolConfigMediaWiring:
    def _make(self, media: MediaConfig | None = None) -> PoolConfig:
        kwargs: dict = {
            "name": "main",
            "main_agent_name": "main",
            "llm": LLMConfig(model="test", api_key="k"),
            "agents": [AgentConfig(name="main", role="main")],
        }
        if media is not None:
            kwargs["media"] = media
        return PoolConfig(**kwargs)

    def test_default_media_present(self) -> None:
        cfg = self._make()
        assert cfg.media.max_image_bytes == 20 * _MB
        assert cfg.media.session_budget_bytes == 500 * _MB

    def test_per_pool_override(self) -> None:
        custom = MediaConfig(max_image_bytes=5 * _MB, session_budget_bytes=100 * _MB)
        cfg = self._make(media=custom)
        assert cfg.media.max_image_bytes == 5 * _MB
        assert cfg.media.session_budget_bytes == 100 * _MB
        # Untouched fields keep defaults.
        assert cfg.media.max_text_doc_bytes == 10 * _MB

    def test_two_pools_distinct_media(self) -> None:
        a = self._make(media=MediaConfig(max_image_bytes=5 * _MB))
        b = self._make(media=MediaConfig(max_image_bytes=50 * _MB))
        assert a.media.max_image_bytes != b.media.max_image_bytes
