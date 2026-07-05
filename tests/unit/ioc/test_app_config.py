import tempfile
from pathlib import Path

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.llm import LLMConfig


class TestAppConfig:
    def test_minimal_app(self) -> None:
        cfg = AppConfig()
        assert cfg.pools == {}
        assert cfg.model is None

    def test_with_pools(self) -> None:
        cfg = AppConfig(
            pools={
                "main": PoolConfig(
                    name="main",
                    main_agent_name="main",
                    llm=LLMConfig(model="gpt-4", api_key="sk-xxx"),
                    agents=[AgentConfig(name="main", role="main")],
                ),
            },
        )
        assert len(cfg.pools) == 1
        assert cfg.pools["main"].main_agent_name == "main"

    def test_from_yaml_minimal(self) -> None:
        """A bare YAML with no pools loads cleanly (pools come from pools/ dir)."""
        yaml_content = """
paths:
  data_dir_name: ".modex"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name

        try:
            cfg = AppConfig.from_yaml(tmp)
            assert cfg.pools == {}
        finally:
            Path(tmp).unlink()

    def test_data_dir_name_defaults_to_modex(self) -> None:
        cfg = AppConfig()
        assert cfg.paths.data_dir_name == ".modex"

    def test_data_dir_name_overridable_from_yaml(self) -> None:
        yaml_content = """
paths:
  data_dir_name: ".custom-modex"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name

        try:
            cfg = AppConfig.from_yaml(tmp)
            assert cfg.paths.data_dir_name == ".custom-modex"
        finally:
            Path(tmp).unlink()
