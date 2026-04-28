"""AgentRuntimeConfig — 运行时配置聚合。

捆绑 hooks、interceptors、control 组件，提供统一配置入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.control.channel import ControlChannel
    from framework.control.checkpoint import CheckpointStore
    from framework.control.event_bus import ControlEventBus
    from framework.control.preset import PresetControlRule
    from framework.hook.abc import HookSpec
    from framework.interceptor.chain import InterceptorChain


@dataclass
class RuntimeControl:
    """控制平面组件聚合。"""

    channel: ControlChannel | None = None
    event_bus: ControlEventBus | None = None
    checkpoint_store: CheckpointStore | None = None
    preset_rules: list[PresetControlRule] = field(default_factory=list)


@dataclass
class AgentRuntimeConfig:
    """运行时配置聚合对象。

    捆绑 hooks、interceptors、control 组件，供 Pipeline / AgentFactory /
    AgentPool / AgentSession 统一使用。

    Usage:
        runtime = AgentRuntimeConfig(
            hooks=[HookSpec(hook=RunLoggingHook(), on_error=HookErrorPolicy.LOG)],
            interceptors=[ControlDrainInterceptor(channel=ctrl_channel)],
            control=RuntimeControl(channel=ctrl_channel, checkpoint_store=store),
        )
    """

    hooks: list[HookSpec] = field(default_factory=list)
    interceptors: list[Any] = field(default_factory=list)
    control: RuntimeControl = field(default_factory=RuntimeControl)
