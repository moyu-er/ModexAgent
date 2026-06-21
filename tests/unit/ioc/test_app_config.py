import tempfile
from pathlib import Path

from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.llm import LLMConfig


class TestAppConfig:
    def test_minimal_app(self) -> None:
        cfg = AppConfig(llm=LLMConfig(model="gpt-4", api_key="sk-xxx"))
        assert cfg.llm.model == "gpt-4"
        assert cfg.agents == []
        assert cfg.mcp is None
        assert cfg.memory is None

    def test_with_agents(self) -> None:
        cfg = AppConfig(
            llm=LLMConfig(model="gpt-4", api_key="sk-xxx"),
            agents=[
                AgentConfig(name="main", max_steps=50),
                AgentConfig(name="worker", max_steps=10),
            ],
        )
        assert len(cfg.agents) == 2
        assert cfg.agents[0].name == "main"

    def test_from_yaml_minimal(self) -> None:
        yaml_content = """
llm:
  model: "gpt-4"
  api_key: "sk-test"
agents:
  - name: main
    max_steps: 30
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False, encoding="utf-8",
        ) as f:
            f.write(yaml_content)
            tmp = f.name

        try:
            cfg = AppConfig.from_yaml(tmp)
            assert cfg.llm.model == "gpt-4"
            assert len(cfg.agents) == 1
            assert cfg.agents[0].max_steps == 30
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
