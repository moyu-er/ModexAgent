from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class StreamingMode(str, Enum):
    """平台支持的流式输出模式。"""

    NATIVE = "native"
    PSEUDO = "pseudo"
    NONE = "none"


class PlatformAdapter(ABC):
    """平台级能力声明与生命周期管理适配器。

    与 turn-level 的 ``InputAdapter`` / ``OutputAdapter`` 不同，
    ``PlatformAdapter`` 负责声明平台级元数据（如是否支持真流式、
    消息编辑、平台启动/停止生命周期等）。

    具体集成时，一个平台通常同时提供：
    - ``PlatformAdapter`` 子类（能力声明）
    - ``InputAdapter`` / ``OutputAdapter`` 子类（I/O 传输）
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """平台标识名称（如 qq, discord, telegram）。"""
        ...

    @property
    def streaming_mode(self) -> StreamingMode:
        """该平台默认的流式输出模式。"""
        return StreamingMode.PSEUDO

    @property
    def supports_message_edit(self) -> bool:
        """是否支持消息编辑（如 Discord、Telegram 支持，QQ 不支持）。"""
        return False

    async def start(self) -> None:
        """启动平台级资源（如连接池、心跳保活等）。"""
        pass

    async def stop(self) -> None:
        """停止平台级资源。"""
        pass


class AdapterRegistry:
    """平台适配器注册表，用于按名称查找已注册的 PlatformAdapter。"""

    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}

    def register(self, adapter: PlatformAdapter) -> None:
        self._adapters[adapter.platform_name] = adapter

    def get(self, platform_name: str) -> PlatformAdapter | None:
        return self._adapters.get(platform_name)

    def list_platforms(self) -> list[str]:
        return list(self._adapters.keys())
