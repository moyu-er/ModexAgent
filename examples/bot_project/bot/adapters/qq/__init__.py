"""QQ Bot V2 Adapters - 使用新架构 InputAdapter/OutputAdapter

基于 Agent Framework V2 架构的 QQ Bot 适配器实现。
使用 AgentPipeline 替代 MessageBus 架构。

新增功能：
- 支持接收 QQ 附件（图片、文件等），下载到本地 media 目录
- 支持发送文件到 QQ（msg_type=7 + base64 上传）
- 支持 markdown 消息格式

Split from the original monolithic ``bot/adapters/qq.py`` into a subpackage
(``_ws_state`` / ``input`` / ``output`` / ``emitter``). Logic is unchanged;
only the module boundary moved.

The package namespace also carries the internal helpers previously importable
from ``qq.py`` (``QQ_FILE_TYPE_IMAGE`` / ``QQ_FILE_TYPE_FILE`` /
``_qq_file_type`` / ``_read_channel_ws`` / ``_write_channel_ws`` /
``_CHANNEL_WS_FILE`` / ``_REGISTRY_DIR``) and ``download_file`` so that
external callers and tests that import or monkeypatch them on
``bot.adapters.qq`` continue to work unchanged.
"""

from __future__ import annotations

# download_file is bound into the package namespace so that
# `monkeypatch.setattr(bot.adapters.qq, "download_file", fake)` continues to
# affect QQInputAdapter._on_message, which resolves it via this package object
# at call time (see input.py: `import bot.adapters.qq as _qq_pkg`).
from bot.adapters.qq._ws_state import (  # noqa: F401
    _CHANNEL_WS_FILE,
    _REGISTRY_DIR,
    QQ_FILE_TYPE_FILE,
    QQ_FILE_TYPE_IMAGE,
    _qq_file_type,
    _read_channel_ws,
    _write_channel_ws,
)
from bot.adapters.qq.emitter import QQBotEmitter, QQEmitterConfig
from bot.adapters.qq.input import QQInputAdapter
from bot.adapters.qq.output import QQOutputAdapter
from bot.utils.media_utils import download_file as download_file

__all__ = [
    "QQInputAdapter",
    "QQOutputAdapter",
    "QQBotEmitter",
    "QQEmitterConfig",
]
