"""Model-config loading helpers extracted from BotService (core.py).

Houses the model.yml + AppConfig post-processing that BotService needs at
construction and initialize() time. Extracted as module-level functions so
core.py stays focused on orchestration; the logic is byte-for-byte the
implementation that lived in ``BotService._load_app_config`` /
``BotService._apply_bot_model_config`` before extraction.
"""

from __future__ import annotations

from pathlib import Path

from bot.service.model_config import BotModelConfig
from modex_agent.ioc.configs.app import AppConfig


def _load_app_config(config_dir: Path) -> AppConfig:
    """Load IOC AppConfig from bot_config.yml.

    框架 AppConfig.from_yaml 不再注入 pool_cfg.llm；模型配置完全由
    BotModelConfig / BotModelProvider 管理。

    Bot 层 model.yml 后处理由调用方经 :func:`_apply_bot_model_config` 完成
    （原来由 ``BotService._load_app_config`` 内联调用 ``self._apply_bot_model_config``）。
    """
    return AppConfig.from_yaml(config_dir / "bot_config.yml")


def _apply_bot_model_config(config_dir: Path, app_config: AppConfig) -> BotModelConfig | None:
    """Bot 层后处理（spec B3）：解析 model.yml 的 models: 块，返回
    BotModelConfig。无论 AppConfig 由本服务加载还是子类预加载传入，都
    必须运行——_bot_model_config 是后续 provider/wiring 的依赖。

    PoolSpec 不再携带 llm；模型配置由 BotModelConfig / BotModelProvider
    独立管理。max_context_tokens 由 wiring 层注入 PoolAssemblyDeps.memory。

    model.yml 缺失时（如框架单测用 config_dir=Path('.') + 合成 app_config，
    或首次部署尚未运行 ``modexbot config``）返回 None；调用方保留既有
    _bot_model_config 值（通常为 None），``_build_default_provider`` 随之
    返回 None，bot 以无模型状态启动，供用户在 WebUI 里完成首次配置。

    ``app_config`` 参数保留以兼容原 ``BotService._apply_bot_model_config``
    签名（self 移除后其余参数原样保留）；当前实现未读取该参数，仅作为
    "AppConfig 由本服务加载或子类预加载传入" 的契约标记。
    """
    model_yml = config_dir / "model.yml"
    if not model_yml.exists():
        return None
    return BotModelConfig.from_yaml(model_yml)
