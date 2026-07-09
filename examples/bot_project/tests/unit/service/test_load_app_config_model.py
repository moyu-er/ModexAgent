# tests/unit/service/test_load_app_config_model.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.core import BotService
from bot.service.model_config import BotModelConfig

from modex_agent.ioc.configs.app import AppConfig


def _write_config(tmp_path: Path) -> Path:
    (tmp_path / "model.yml").write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  max_context_tokens: 99999\n'
        '  providers:\n    - {key: a, name: "A", url: https://u/v, api_key: KEY, models: [{name: M1, model: openai/m1, capabilities: [text, image]}]}\n',
        encoding="utf-8",
    )
    (tmp_path / "bot_config.yml").write_text(
        'multi_agent: {default_pool: main}\n'
        'paths: {data_dir_name: .modex}\n'
        'workspace: {enabled: false}\n',
        encoding="utf-8",
    )
    pools = tmp_path / "pools" / "main"
    pools.mkdir(parents=True)
    (pools / "pool.yml").write_text(
        'name: main\n'
        'main_agent_name: main\n'
        'memory:\n  session: {max_token_ratio: 0.8}\n'
        'agents:\n  - {name: main, role: main, max_steps: 5}\n',
        encoding="utf-8",
    )
    return tmp_path


def test_load_app_config_injects_default_llm_and_max_context(tmp_path: Path) -> None:
    config_dir = _write_config(tmp_path)
    svc = BotService(
        config_dir=config_dir,
        input_adapter=object(),
        output_adapter=object(),
        emitter_factory=lambda sid: None,
    )
    app_cfg = svc._load_app_config()
    assert isinstance(app_cfg, AppConfig)
    pool = app_cfg.pools["main"]
    # Model config is now owned by BotModelConfig, not PoolConfig.llm.
    assert svc._bot_model_config is not None
    assert isinstance(svc._bot_model_config, BotModelConfig)
    resolved = svc._bot_model_config.default_resolved()
    assert resolved.model.model == "openai/m1"
    assert resolved.provider.api_key == "KEY"
    assert resolved.provider.url == "https://u/v"
    # max_context_tokens is injected into memory.session.max_context_tokens.
    assert pool.memory.session.max_context_tokens == 99999
    # BotModelConfig is cached.
    assert svc._bot_model_config.default_resolved().model.model == "openai/m1"


def test_pre_supplied_app_config_still_applies_bot_model_config(tmp_path: Path) -> None:
    """Subclasses (WebUIService/QQBotService) pre-load AppConfig and pass it in.

    _bot_model_config must still be populated (and pools post-processed) in
    __init__ — otherwise initialize()'s `if self._app_config is None` guard
    skips _load_app_config entirely and _build_default_provider crashes on
    the `assert self._bot_model_config is not None`. Regression test for the
    production startup path (no _load_app_config call here).
    """
    from modex_agent.ioc.configs.app import AppConfig as _AppConfig

    config_dir = _write_config(tmp_path)
    pre_loaded = _AppConfig.from_yaml(config_dir / "bot_config.yml")
    svc = BotService(
        config_dir=config_dir,
        input_adapter=object(),
        output_adapter=object(),
        emitter_factory=lambda sid: None,
        app_config=pre_loaded,
    )
    # No _load_app_config() call — __init__ must have applied the post-process.
    assert svc._bot_model_config is not None
    assert svc._bot_model_config.default_resolved().model.model == "openai/m1"
    # The pre-supplied AppConfig's pools were mutated in place (max_context_tokens).
    assert pre_loaded.pools["main"].memory.session.max_context_tokens == 99999
