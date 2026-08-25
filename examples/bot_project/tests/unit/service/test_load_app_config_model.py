from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service._model_config_loader import _apply_bot_model_config, _load_app_config
from bot.service.core import BotService
from bot.service.model_config import BotModelConfig

from modex_agent.core.constants import InterfaceFormat
from modex_agent.ioc.configs.app import AppConfig


def _write_config(tmp_path: Path) -> Path:
    (tmp_path / "model.yml").write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  max_context_tokens: 99999\n'
        '  providers:\n    - {key: a, name: "A", url: https://u/v, api_key: KEY, models: [{name: M1, model: openai/m1, capabilities: [text, image]}]}\n',
        encoding="utf-8",
    )
    (tmp_path / "bot_config.yml").write_text(
        "multi_agent: {}\npaths: {data_dir_name: .modex}\nworkspace: {enabled: false}\n",
        encoding="utf-8",
    )
    pools = tmp_path / "pools" / "main"
    pools.mkdir(parents=True)
    (pools / "pool.yml").write_text(
        "name: main\n"
        "root_agent_name: main\n"
        "memory:\n  session: {max_token_ratio: 0.8}\n"
        "agents:\n  - {name: main, role: main, max_steps: 5}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_load_app_config_injects_bot_model_config(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path)
    svc = BotService(
        config_dir=config_dir,
        input_adapter=object(),
        output_adapter=object(),
        emitter_factory=lambda _sid, *, pool: None,
    )
    app_cfg = _load_app_config(config_dir)
    svc._bot_model_config = _apply_bot_model_config(config_dir, app_cfg)
    assert isinstance(app_cfg, AppConfig)
    assert svc._bot_model_config is not None
    assert isinstance(svc._bot_model_config, BotModelConfig)
    resolved = svc._bot_model_config.default_resolved()
    assert resolved.model.model == "m1"
    assert resolved.provider.api_key == "KEY"
    assert resolved.provider.base_url == "https://u/v"
    assert resolved.provider.interface_format == InterfaceFormat.OPENAI_COMPATIBLE


def test_pre_supplied_app_config_still_applies_bot_model_config(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path)
    pre_loaded = AppConfig.from_yaml(config_dir / "bot_config.yml")
    svc = BotService(
        config_dir=config_dir,
        input_adapter=object(),
        output_adapter=object(),
        emitter_factory=lambda _sid, *, pool: None,
        app_config=pre_loaded,
    )
    assert svc._bot_model_config is not None
    assert svc._bot_model_config.default_resolved().model.model == "m1"
    assert (
        svc._bot_model_config.default_resolved().provider.interface_format
        == InterfaceFormat.OPENAI_COMPATIBLE
    )
