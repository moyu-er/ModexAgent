import tempfile
from pathlib import Path

from modex_agent.ioc.configs.app import AppConfig


class TestAppConfig:
    def test_minimal_app(self) -> None:
        cfg = AppConfig()
        assert "pools" not in cfg.model_fields
        assert cfg.model is None

    def test_from_yaml_minimal(self) -> None:
        """A bare YAML loads cleanly; pools are not read from config/pools/."""
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
            assert "pools" not in cfg.model_fields
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

    def test_multi_agent_has_no_default_pool(self) -> None:
        cfg = AppConfig()
        assert "default_pool" not in cfg.multi_agent.model_fields

    def test_from_yaml_ignores_pools_directory(self) -> None:
        """AppConfig.from_yaml() no longer reads config/pools/*."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_yml = tmp_path / "bot_config.yml"
            config_yml.write_text("paths:\n  data_dir_name: .modex\n", encoding="utf-8")
            # Create a pools directory with a pool.yml; it should be ignored.
            pools_dir = tmp_path / "pools"
            pools_dir.mkdir()
            (pools_dir / "default").mkdir()
            (pools_dir / "default" / "pool.yml").write_text(
                "max_steps: 50\n", encoding="utf-8"
            )

            cfg = AppConfig.from_yaml(config_yml)
            assert "pools" not in cfg.model_fields
