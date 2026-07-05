"""TDD tests for the refactored PoolConfig + AppConfig.from_yaml pool loading (Task 1.5)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.pool import PoolConfig


class TestPoolConfigNameFields:
    def test_pool_has_name_and_main_agent_name(self) -> None:
        cfg = PoolConfig(
            name="main",
            main_agent_name="main",
            llm=LLMConfig(model="gpt-4", api_key="k"),
            agents=[AgentConfig(name="main", role="main")],
        )
        assert cfg.name == "main"
        assert cfg.main_agent_name == "main"

    def test_name_can_differ_from_main_agent_name(self) -> None:
        cfg = PoolConfig(
            name="coding",
            main_agent_name="coder",
            llm=LLMConfig(model="gpt-4", api_key="k"),
            agents=[AgentConfig(name="coder", role="main")],
        )
        assert cfg.name != cfg.main_agent_name

    def test_extra_forbid_rejects_terminal(self) -> None:
        with pytest.raises(ValidationError):
            PoolConfig(
                name="main",
                main_agent_name="main",
                llm=LLMConfig(model="gpt-4", api_key="k"),
                agents=[AgentConfig(name="main", role="main")],
                terminal={"storage_dir": "x"},  # type: ignore[arg-type]
            )


class TestFromYamlPoolLoading:
    def _setup(self, tmp_path: Path) -> Path:
        """bot_config.yml + pools live under <root>/config/ (real layout)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "bot_config.yml").write_text("paths: {}\n", encoding="utf-8")
        return config_dir

    def test_pool_name_is_directory_name(self, tmp_path: Path) -> None:
        """Pool identity IS the directory name — a YAML ``name:`` is ignored,
        and the loaded pool name always equals the directory name."""
        config_dir = self._setup(tmp_path)
        pools_dir = config_dir / "pools" / "main"
        pools_dir.mkdir(parents=True)
        (pools_dir / "pool.yml").write_text(
            "name: not-main\n"  # ignored — dir name wins
            "main_agent_name: main\n"
            "llm:\n  model: gpt-4\n  api_key: k\n"
            "agents:\n  - name: main\n    role: main\n",
            encoding="utf-8",
        )
        cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")
        assert "main" in cfg.pools
        assert cfg.pools["main"].name == "main"  # dir name, not the YAML value
        assert "not-main" not in cfg.pools

    def test_main_agent_name_sets_main_agent_name(self, tmp_path: Path) -> None:
        """Flat pool.yml: ``main_agent_name`` IS the main agent's name (single
        source — no separate ``agents:[].name`` to mismatch)."""
        config_dir = self._setup(tmp_path)
        pools_dir = config_dir / "pools" / "main"
        pools_dir.mkdir(parents=True)
        (pools_dir / "pool.yml").write_text(
            "main_agent_name: lead\n"
            "llm:\n  model: gpt-4\n  api_key: k\n",
            encoding="utf-8",
        )
        cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")
        assert cfg.pools["main"].main_agent_name == "lead"
        assert cfg.pools["main"].agents[0].name == "lead"

    def test_name_field_optional_derived_from_dir(self, tmp_path: Path) -> None:
        """No ``name:`` in YAML — pool loads with name = directory name."""
        config_dir = self._setup(tmp_path)
        pools_dir = config_dir / "pools" / "main"
        pools_dir.mkdir(parents=True)
        (pools_dir / "pool.yml").write_text(
            "llm:\n  model: gpt-4\n  api_key: k\n",
            encoding="utf-8",
        )
        cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")
        assert cfg.pools["main"].name == "main"

    def test_main_agent_name_defaults_to_dir(self, tmp_path: Path) -> None:
        """Omitting ``main_agent_name`` — it defaults to the directory name."""
        config_dir = self._setup(tmp_path)
        pools_dir = config_dir / "pools" / "main"
        pools_dir.mkdir(parents=True)
        (pools_dir / "pool.yml").write_text(
            "llm:\n  model: gpt-4\n  api_key: k\n",
            encoding="utf-8",
        )
        cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")
        pool = cfg.pools["main"]
        assert pool.main_agent_name == "main"  # defaulted from dir
        assert pool.agents[0].name == "main"

    def test_flat_main_agent_fields_lifted(self, tmp_path: Path) -> None:
        """Main-agent editable fields at top level are lifted onto the main agent."""
        config_dir = self._setup(tmp_path)
        pools_dir = config_dir / "pools" / "main"
        pools_dir.mkdir(parents=True)
        (pools_dir / "pool.yml").write_text(
            "llm:\n  model: gpt-4\n  api_key: k\n"
            "max_steps: 77\n"
            "tool_preset: read_only\n"
            "mcp:\n  - playwright\n",
            encoding="utf-8",
        )
        cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")
        main = cfg.pools["main"].agents[0]
        assert main.max_steps == 77
        assert main.tool_preset.value == "read_only"
        assert main.mcp == ["playwright"]
