"""Tests for GlobalModelConfig and its loading through AppConfig.from_yaml."""

from __future__ import annotations

import tempfile
from pathlib import Path

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.model import GlobalModelConfig


class TestGlobalModelConfig:
    def test_to_llm_dict_renames_url_to_base_url(self) -> None:
        cfg = GlobalModelConfig(
            url="https://api.example.com/v1",
            api_key="sk-xxx",
            model="openai/foo",
            capabilities=["text", "image"],
        )
        d = cfg.to_llm_dict()
        assert d["base_url"] == "https://api.example.com/v1"
        assert "url" not in d
        assert d["model"] == "openai/foo"
        assert d["api_key"] == "sk-xxx"
        assert d["capabilities"] == ["text", "image"]

    def test_capabilities_default_is_text_only(self) -> None:
        assert GlobalModelConfig().to_llm_dict()["capabilities"] == ["text"]


def _write_config_tree(tmp: Path, *, pool_llm: str | None = None, with_model: bool) -> Path:
    """Build a config/ tree and return the bot_config.yml path.

    Uses the dir-based pool layout (``pools/<name>/pool.yml``) that
    ``AppConfig.from_yaml`` scans — not the legacy single ``pools/<name>.yml``
    file, which the loader ignores.
    """
    (tmp / "pools" / "main").mkdir(parents=True)
    (tmp / "bot_config.yml").write_text("workspace:\n  enabled: false\n", encoding="utf-8")
    if with_model:
        (tmp / "model.yml").write_text(
            "model:\n"
            "  url: https://api.example.com/v1\n"
            "  api_key: sk-global\n"
            "  model: openai/global-model\n"
            "  capabilities: [text, image]\n",
            encoding="utf-8",
        )
    pool = "name: main\nmain_agent_name: main\nagents:\n  - name: main\n    role: main\n"
    if pool_llm is not None:
        pool = pool_llm + pool
    (tmp / "pools" / "main" / "pool.yml").write_text(pool, encoding="utf-8")
    return tmp / "bot_config.yml"


class TestGlobalModelInjection:
    def test_global_model_loaded_into_app_config(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cfg_path = _write_config_tree(Path(t), with_model=True)
            cfg = AppConfig.from_yaml(cfg_path)
            assert cfg.model is not None
            assert cfg.model.model == "openai/global-model"
            assert cfg.model.url == "https://api.example.com/v1"
            assert cfg.model.api_key == "sk-global"
            assert "image" in cfg.model.capabilities

    def test_pool_llm_block_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cfg_path = _write_config_tree(
                Path(t),
                pool_llm="llm:\n  model: openai/pool-override\n",
                with_model=True,
            )
            cfg = AppConfig.from_yaml(cfg_path)
            assert cfg.model is not None
            assert cfg.model.model == "openai/global-model"
            assert "main" in cfg.pools

    def test_no_model_yml_loads_pool_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            cfg_path = _write_config_tree(Path(t), with_model=False)
            cfg = AppConfig.from_yaml(cfg_path)
            assert cfg.model is None
            assert "main" in cfg.pools
            assert cfg.pools["main"].main_agent_name == "main"
